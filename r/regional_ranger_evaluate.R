# MEOW-14 红树林林下地形：完整 train70 拟合 + 锁定随机 test30 验证。
# 输出是对 GEDI 聚合标签的内部随机验证，不代表独立 LiDAR/RTK 外部精度。

args <- commandArgs(trailingOnly = TRUE)
arg_value <- function(name, default = NULL) {
  index <- match(name, args)
  if (is.na(index) || index == length(args)) return(default)
  args[[index + 1]]
}
manifest_path <- arg_value("--manifest")
tuning_dir <- arg_value("--tuning-dir")
output_dir <- arg_value("--output-dir")
run_mode <- arg_value("--run-mode", "full")
seed <- as.integer(arg_value("--seed", "42"))
requested_regions <- arg_value("--regions", "")
prediction_batch_rows <- as.integer(arg_value("--prediction-batch-rows", "100000"))
save_models <- tolower(arg_value("--save-models", "false")) == "true"
if (is.null(manifest_path) || is.null(tuning_dir) || is.null(output_dir)) stop("必须提供 --manifest、--tuning-dir 和 --output-dir。")

required_packages <- c("ranger", "data.table", "ggplot2")
missing_packages <- required_packages[!vapply(required_packages, requireNamespace, logical(1), quietly = TRUE)]
if (length(missing_packages) > 0) stop(paste("缺少 R 包：", paste(missing_packages, collapse = ", ")))
library(ranger)
library(data.table)
library(ggplot2)
predictors <- sprintf("A%02d", 0:63)
target <- "elev_median"
needed <- c(predictors, target, "elev_count", "elev_iqr")
manifest <- fread(manifest_path)
if (!all(c("region_code", "train_csv", "test_csv") %in% names(manifest))) stop("区域清单缺少必需字段。")
if (nzchar(requested_regions)) {
  requested <- trimws(strsplit(requested_regions, ",", fixed = TRUE)[[1]])
  manifest <- manifest[region_code %in% requested]
  if (nrow(manifest) == 0) stop("--regions 未匹配任何区域代码。")
}
if (run_mode == "quick") manifest <- head(manifest, 1)
dir.create(output_dir, recursive = TRUE, showWarnings = FALSE)
dir.create(file.path(output_dir, "figures"), recursive = TRUE, showWarnings = FALSE)
if (save_models) dir.create(file.path(output_dir, "models"), recursive = TRUE, showWarnings = FALSE)
formula <- as.formula(paste(target, "~", paste(predictors, collapse = " + ")))
threads <- max(1L, parallel::detectCores() - 1L)

metric_row <- function(observed, predicted) {
  error <- predicted - observed
  n <- length(error)
  sse <- sum(error ^ 2)
  sum_y <- sum(observed)
  sum_y2 <- sum(observed ^ 2)
  sst <- sum((observed - mean(observed)) ^ 2)
  data.table(n=n, rmse=sqrt(mean(error ^ 2)), mae=mean(abs(error)), bias=mean(error),
             r2=ifelse(sst > 0, 1 - sse / sst, NA_real_), sse=sse,
             sum_abs_error=sum(abs(error)), sum_error=sum(error), sum_y=sum_y, sum_y2=sum_y2)
}

summary_rows <- list()
preview_rows <- list()
strata_rows <- list()
summary_index <- 1L
preview_index <- 1L
strata_index <- 1L
for (row_index in seq_len(nrow(manifest))) {
  item <- manifest[row_index]
  code <- as.character(item$region_code)
  train_path <- as.character(item$train_csv)
  test_path <- as.character(item$test_csv)
  params_path <- file.path(tuning_dir, code, "ranger_top10_mean_params_for_gee.csv")
  if (!file.exists(params_path)) stop(sprintf("区域 %s 找不到调参参数：%s", code, params_path))
  if (!file.exists(train_path) || !file.exists(test_path)) stop(sprintf("区域 %s 的 train/test CSV 缺失。", code))
  params <- fread(params_path)
  if (nrow(params) != 1) stop(sprintf("区域 %s 的参数文件必须恰有一行。", code))
  message(sprintf("\n========== 区域 %s：完整 train70 拟合与 test30 评估 ==========" , code))
  train <- fread(train_path, select=needed, showProgress=FALSE)
  test <- fread(test_path, select=needed, showProgress=FALSE)
  train <- train[complete.cases(train[, ..needed])]
  test <- test[complete.cases(test[, ..needed])]
  if (nrow(train) < 2 || nrow(test) < 2) stop(sprintf("区域 %s 的有效 train/test 样本不足。", code))
  fit <- ranger(
    formula=formula, data=train, num.trees=params$numberOfTrees, mtry=params$variablesPerSplit,
    sample.fraction=params$bagFraction, min.node.size=params$minLeafPopulation,
    replace=FALSE, seed=params$seed, num.threads=threads, importance="none"
  )
  if (save_models) saveRDS(fit, file.path(output_dir, "models", paste0(code, "_ranger_train70.rds")))
  prediction <- numeric(nrow(test))
  for (start in seq(1L, nrow(test), by=prediction_batch_rows)) {
    end <- min(nrow(test), start + prediction_batch_rows - 1L)
    prediction[start:end] <- predict(fit, test[start:end, ..predictors])$predictions
  }
  observed <- test[[target]]
  error <- prediction - observed
  metric <- metric_row(observed, prediction)
  metric[, `:=`(region_code=code, train_rows=nrow(train), test_rows=nrow(test),
                numberOfTrees=params$numberOfTrees, variablesPerSplit=params$variablesPerSplit,
                bagFraction=params$bagFraction, minLeafPopulation=params$minLeafPopulation)]
  summary_rows[[summary_index]] <- metric
  summary_index <- summary_index + 1L
  count_class <- cut(test$elev_count, breaks=c(-Inf, 1, 2, 5, 10, Inf), labels=c("1", "2", "3-5", "6-10", ">10"), right=TRUE)
  iqr_breaks <- unique(as.numeric(quantile(test$elev_iqr, probs=c(0, .25, .5, .75, 1), na.rm=TRUE)))
  iqr_class <- if (length(iqr_breaks) >= 2) cut(test$elev_iqr, breaks=iqr_breaks, include.lowest=TRUE, dig.lab=5) else factor("constant")
  grouped <- data.table(count_group=as.character(count_class), iqr_group=as.character(iqr_class), observed=observed, predicted=prediction)
  by_count <- grouped[, metric_row(observed, predicted), by=.(group=count_group)]
  by_count[, `:=`(region_code=code, diagnostic="elev_count")]
  by_iqr <- grouped[, metric_row(observed, predicted), by=.(group=iqr_group)]
  by_iqr[, `:=`(region_code=code, diagnostic="elev_iqr")]
  strata_rows[[strata_index]] <- by_count
  strata_index <- strata_index + 1L
  strata_rows[[strata_index]] <- by_iqr
  strata_index <- strata_index + 1L
  preview_n <- min(10000L, nrow(test))
  set.seed(seed + sum(utf8ToInt(code)) * 3001L)
  preview_ids <- if (preview_n == nrow(test)) seq_len(nrow(test)) else sample.int(nrow(test), preview_n, replace=FALSE)
  preview_rows[[preview_index]] <- data.table(region_code=code, observed=observed[preview_ids], predicted=prediction[preview_ids],
                                               residual=error[preview_ids], elev_count=test$elev_count[preview_ids], elev_iqr=test$elev_iqr[preview_ids])
  preview_index <- preview_index + 1L
  region_dir <- file.path(output_dir, code)
  dir.create(region_dir, recursive=TRUE, showWarnings=FALSE)
  fwrite(metric, file.path(region_dir, "local_train70_test30_metrics.csv"))
  fwrite(data.table(residual_q001=quantile(error, .001), residual_q01=quantile(error, .01), residual_q05=quantile(error, .05),
                     residual_median=median(error), residual_q95=quantile(error, .95), residual_q99=quantile(error, .99), residual_q999=quantile(error, .999)),
         file.path(region_dir, "residual_quantiles.csv"))
}

summary <- rbindlist(summary_rows, fill=TRUE)
summary <- summary[, .(region_code, train_rows, test_rows, rmse, mae, bias, r2, n, sse, sum_abs_error, sum_error, sum_y, sum_y2,
                       numberOfTrees, variablesPerSplit, bagFraction, minLeafPopulation)]
fwrite(summary, file.path(output_dir, "regional_evaluation_summary.csv"))
total_n <- sum(summary$n)
total_sse <- sum(summary$sse)
total_sum_y <- sum(summary$sum_y)
total_sum_y2 <- sum(summary$sum_y2)
total_sst <- total_sum_y2 - (total_sum_y ^ 2 / total_n)
overall <- data.table(metric_scope=c("weighted_all_test_pixels", "macro_mean_of_14_regions"),
                      rmse=c(sqrt(total_sse / total_n), mean(summary$rmse)),
                      mae=c(sum(summary$sum_abs_error) / total_n, mean(summary$mae)),
                      bias=c(sum(summary$sum_error) / total_n, mean(summary$bias)),
                      r2=c(ifelse(total_sst > 0, 1 - total_sse / total_sst, NA_real_), mean(summary$r2, na.rm=TRUE)),
                      test_rows=c(total_n, NA_real_))
fwrite(overall, file.path(output_dir, "regional_evaluation_overall_metrics.csv"))
strata <- rbindlist(strata_rows, fill=TRUE)
fwrite(strata, file.path(output_dir, "regional_test_diagnostics_by_label_stability.csv"))
preview <- rbindlist(preview_rows, fill=TRUE)
fwrite(preview, file.path(output_dir, "regional_test_prediction_preview.csv"))
scatter <- ggplot(preview, aes(observed, predicted)) + geom_bin2d(bins=65) +
  geom_abline(slope=1, intercept=0, color="white", linewidth=.35) + facet_wrap(~region_code, scales="free") +
  scale_fill_viridis_c(name="样本数") + labs(x="GEDI 聚合 elev_median (m)", y="本地 ranger 预测 (m)", title="MEOW-14 随机 test30 内部验证") + theme_bw(base_size=10)
ggsave(file.path(output_dir, "figures", "regional_observed_predicted.png"), scatter, width=12, height=9, dpi=240)
residual_plot <- ggplot(preview, aes(region_code, residual)) + geom_boxplot(outlier.size=.15) + geom_hline(yintercept=0, color="#d95f0e") +
  labs(x="MEOW-14 区域", y="预测残差 (m)", title="区域 test30 残差分布") + theme_bw(base_size=10)
ggsave(file.path(output_dir, "figures", "regional_residual_boxplot.png"), residual_plot, width=12, height=5, dpi=240)
cat("REGIONAL_RANGER_EVALUATION_OK\n")
