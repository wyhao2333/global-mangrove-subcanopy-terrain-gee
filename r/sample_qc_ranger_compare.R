# GEDI 样本筛选候选规则的固定参数内部对照。
# 这不是 LiDAR/RTK 外部验证；全部指标都只描述对 GEDI 聚合标签的可预测性。
args <- commandArgs(trailingOnly = TRUE)
arg_value <- function(name, default = NULL) {
  index <- match(name, args)
  if (is.na(index) || index == length(args)) return(default)
  args[[index + 1]]
}
manifest_path <- arg_value("--manifest")
output_dir <- arg_value("--output-dir")
seed <- as.integer(arg_value("--seed", "42"))
trees <- as.integer(arg_value("--trees", "300"))
mtry <- as.integer(arg_value("--mtry", "16"))
bag_fraction <- as.numeric(arg_value("--bag-fraction", "0.632"))
min_node_size <- as.integer(arg_value("--min-node-size", "5"))
if (is.null(manifest_path) || is.null(output_dir)) stop("必须提供 --manifest 和 --output-dir。")

required_packages <- c("ranger", "data.table", "ggplot2")
missing_packages <- required_packages[!vapply(required_packages, requireNamespace, logical(1), quietly = TRUE)]
if (length(missing_packages) > 0) stop(paste("缺少 R 包:", paste(missing_packages, collapse = ", ")))
library(ranger)
library(data.table)
library(ggplot2)

predictors <- sprintf("A%02d", 0:63)
needed <- c("sample_id", "REG_CODE", "split", "elev_median", "elev_count", "elev_iqr", predictors)
candidates <- c("base_qa", "range_candidate", "provisional_screened")
manifest <- fread(manifest_path)
if (!all(c("region_code", "status", "base_test_csv", "repeat_proxy_csv") %in% names(manifest))) stop("QC 模型清单缺少必要字段。")
manifest <- manifest[status == "ready"]
if (nrow(manifest) == 0) stop("没有可运行的 QC 候选模型区域。")
dir.create(output_dir, recursive = TRUE, showWarnings = FALSE)
dir.create(file.path(output_dir, "figures"), recursive = TRUE, showWarnings = FALSE)

metric_row <- function(observed, predicted) {
  error <- predicted - observed
  sst <- sum((observed - mean(observed)) ^ 2)
  data.table(
    n = length(error), rmse = sqrt(mean(error ^ 2)), mae = mean(abs(error)), bias = mean(error),
    r2 = ifelse(sst > 0, 1 - sum(error ^ 2) / sst, NA_real_),
    residual_q01 = quantile(error, 0.01), residual_q50 = median(error), residual_q99 = quantile(error, 0.99)
  )
}

read_data <- function(path) {
  data <- fread(path, select = needed, showProgress = FALSE)
  data <- data[complete.cases(data[, ..needed])]
  if (nrow(data) < 2) stop(paste("有效样本不足:", path))
  data
}

summary_rows <- list()
row_index <- 1L
threads <- max(1L, parallel::detectCores() - 1L)
for (item_index in seq_len(nrow(manifest))) {
  item <- manifest[item_index]
  code <- as.character(item$region_code)
  base_test <- read_data(as.character(item$base_test_csv))
  proxy_test <- read_data(as.character(item$repeat_proxy_csv))
  message(sprintf("\n========== 样本 QC 对照: %s ==========", code))
  for (candidate in candidates) {
    train_column <- paste0(candidate, "_train_csv")
    if (!train_column %in% names(item) || !nzchar(as.character(item[[train_column]]))) next
    train <- read_data(as.character(item[[train_column]]))
    set.seed(seed + sum(utf8ToInt(paste(code, candidate))))
    fit <- ranger(
      dependent.variable.name = "elev_median", data = train[, c("elev_median", predictors), with = FALSE],
      num.trees = trees, mtry = mtry, sample.fraction = bag_fraction, min.node.size = min_node_size,
      replace = FALSE, seed = seed, num.threads = threads, importance = "none"
    )
    for (scope in c("base_test30", "repeat_high_confidence_test30")) {
      test <- if (scope == "base_test30") base_test else proxy_test
      predicted <- predict(fit, test[, ..predictors])$predictions
      result <- metric_row(test$elev_median, predicted)
      result[, `:=`(
        region_code = code, candidate = candidate, test_scope = scope,
        train_rows = nrow(train), number_of_trees = trees, mtry = mtry,
        bag_fraction = bag_fraction, min_node_size = min_node_size
      )]
      summary_rows[[row_index]] <- result
      row_index <- row_index + 1L
    }
  }
}
summary <- rbindlist(summary_rows, fill = TRUE)
setcolorder(summary, c("region_code", "candidate", "test_scope", "train_rows", "n", "rmse", "mae", "bias", "r2"))
fwrite(summary, file.path(output_dir, "qc_candidate_model_metrics_by_region.csv"))

overall <- summary[, .(
  regions = uniqueN(region_code), n = sum(n), rmse_macro = mean(rmse), mae_macro = mean(mae),
  bias_macro = mean(bias), r2_macro = mean(r2, na.rm = TRUE), rmse_weighted = sqrt(weighted.mean(rmse ^ 2, n)),
  mae_weighted = weighted.mean(mae, n), bias_weighted = weighted.mean(bias, n)
), by = .(candidate, test_scope)]
fwrite(overall, file.path(output_dir, "qc_candidate_model_metrics_overall.csv"))

plot <- ggplot(overall, aes(candidate, rmse_macro, fill = test_scope)) +
  geom_col(position = position_dodge(width = .75), width = .65) +
  labs(
    title = "候选样本规则的 ranger 内部对照",
    subtitle = "指标仅表示对 GEDI 聚合标签的可预测性，不是 LiDAR/RTK 外部地形精度",
    x = "训练样本规则", y = "14 区宏平均 RMSE (m)", fill = "测试集"
  ) +
  theme_bw(base_size = 10) +
  theme(panel.grid.major.x = element_blank(), legend.position = "top")
ggsave(file.path(output_dir, "figures", "01_qc_candidate_internal_rmse.png"), plot, width = 8.2, height = 5.1, dpi = 220)

message("QC ranger 候选对照完成: ", file.path(output_dir, "qc_candidate_model_metrics_overall.csv"))
