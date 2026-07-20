# 全球红树林林下地形随机森林回归调参。
# 复用 Science Supplement 的核心思想：ranger 超参数网格、重复 OOB 评估、前10参数均值映射到 GEE。

args <- commandArgs(trailingOnly = TRUE)
arg_value <- function(name, default = NULL) {
  index <- match(name, args)
  if (is.na(index) || index == length(args)) return(default)
  args[[index + 1]]
}

train_csv <- arg_value("--train-pool")
test_csv <- arg_value("--test-pool")
output_dir <- arg_value("--output-dir")
run_mode <- arg_value("--run-mode", "full")
repeats <- as.integer(arg_value("--repeats", "5"))
rows_per_repeat <- as.integer(arg_value("--rows-per-repeat", "200000"))
seed <- as.integer(arg_value("--seed", "42"))

if (is.null(train_csv) || is.null(test_csv) || is.null(output_dir)) {
  stop("必须提供 --train-pool、--test-pool 和 --output-dir。")
}

required_packages <- c("ranger", "data.table", "ggplot2")
missing_packages <- required_packages[!vapply(required_packages, requireNamespace, logical(1), quietly = TRUE)]
if (length(missing_packages) > 0) stop(paste("缺少 R 包：", paste(missing_packages, collapse = ", ")))
library(ranger)
library(data.table)
library(ggplot2)

dir.create(output_dir, recursive = TRUE, showWarnings = FALSE)
figure_dir <- file.path(output_dir, "figures")
dir.create(figure_dir, recursive = TRUE, showWarnings = FALSE)

predictors <- sprintf("A%02d", 0:63)
target <- "elev_median"
train <- fread(train_csv)
test <- fread(test_csv)
needed <- c(predictors, target)
if (length(setdiff(needed, names(train))) > 0 || length(setdiff(needed, names(test))) > 0) {
  stop("训练池或测试池缺少 elev_median / A00-A63 字段。")
}
train <- train[complete.cases(train[, ..needed])]
test <- test[complete.cases(test[, ..needed])]
if (nrow(train) < 2 || nrow(test) < 2) stop("训练池或测试池有效样本太少。")

grid <- expand.grid(
  num.trees = c(50, 100, 200, 300, 400),
  mtry = c(8, 16, 24, 32),
  sample.fraction = c(0.5, 0.632, 0.8),
  min.node.size = c(1, 3, 5, 10),
  KEEP.OUT.ATTRS = FALSE,
  stringsAsFactors = FALSE
)
grid$candidate_id <- seq_len(nrow(grid))
fwrite(as.data.table(grid), file.path(output_dir, "ranger_hypergrid_240.csv"))
if (run_mode == "quick") grid <- grid[seq_len(min(5, nrow(grid))), ]

sample_size <- min(rows_per_repeat, nrow(train))
test_size <- min(rows_per_repeat, nrow(test))
set.seed(seed + 2001)
test_eval <- test[sample.int(nrow(test), test_size)]
formula <- as.formula(paste(target, "~", paste(predictors, collapse = " + ")))
threads <- max(1L, parallel::detectCores() - 1L)

result_rows <- list()
row_index <- 1L
for (repeat_id in seq_len(repeats)) {
  set.seed(seed + repeat_id)
  train_sample <- train[sample.int(nrow(train), sample_size)]
  for (grid_index in seq_len(nrow(grid))) {
    params <- grid[grid_index, ]
    message(sprintf("重复 %d/%d；参数 %d/%d；trees=%d, mtry=%d, bag=%.3f, minnode=%d",
                    repeat_id, repeats, grid_index, nrow(grid), params$num.trees,
                    params$mtry, params$sample.fraction, params$min.node.size))
    fit <- ranger(
      formula = formula,
      data = train_sample,
      num.trees = params$num.trees,
      mtry = params$mtry,
      sample.fraction = params$sample.fraction,
      min.node.size = params$min.node.size,
      seed = seed + repeat_id,
      num.threads = threads,
      importance = "none"
    )
    result_rows[[row_index]] <- data.table(
      candidate_id = params$candidate_id,
      repeat_id = repeat_id,
      num.trees = params$num.trees,
      mtry = params$mtry,
      sample.fraction = params$sample.fraction,
      min.node.size = params$min.node.size,
      oob_rmse = sqrt(fit$prediction.error)
    )
    row_index <- row_index + 1L
  }
}

results <- rbindlist(result_rows)
fwrite(results, file.path(output_dir, "ranger_repeated_oob_results.csv"))
ranking <- results[, .(
  oob_rmse_mean = mean(oob_rmse),
  oob_rmse_sd = sd(oob_rmse),
  repeats = .N
), by = .(candidate_id, num.trees, mtry, sample.fraction, min.node.size)][order(oob_rmse_mean, oob_rmse_sd)]
ranking[, rank := seq_len(.N)]
fwrite(ranking, file.path(output_dir, "ranger_oob_ranking.csv"))
top10 <- head(ranking, 10L)
fwrite(top10, file.path(output_dir, "ranger_top10_params.csv"))

gee_params <- data.table(
  top_n = nrow(top10),
  numberOfTrees = round(mean(top10$num.trees)),
  variablesPerSplit = round(mean(top10$mtry)),
  bagFraction = round(mean(top10$sample.fraction), 3),
  minLeafPopulation = round(mean(top10$min.node.size)),
  seed = seed,
  selection_metric = "repeated_oob_rmse_mean"
)
fwrite(gee_params, file.path(output_dir, "ranger_top10_mean_params_for_gee.csv"))

# 使用前10平均参数在每个独立训练子样本上重新拟合，再预测固定测试池，
# 报告独立测试精度均值和标准差，不把测试集用于网格排名。
validation_rows <- list()
for (repeat_id in seq_len(repeats)) {
  set.seed(seed + repeat_id)
  train_sample <- train[sample.int(nrow(train), sample_size)]
  fit <- ranger(
    formula = formula,
    data = train_sample,
    num.trees = gee_params$numberOfTrees,
    mtry = gee_params$variablesPerSplit,
    sample.fraction = gee_params$bagFraction,
    min.node.size = gee_params$minLeafPopulation,
    seed = seed + repeat_id,
    num.threads = threads,
    importance = "none"
  )
  predicted <- predict(fit, test_eval)$predictions
  error <- predicted - test_eval[[target]]
  ss_total <- sum((test_eval[[target]] - mean(test_eval[[target]]))^2)
  validation_rows[[repeat_id]] <- data.table(
    repeat_id = repeat_id,
    rmse = sqrt(mean(error^2)),
    mae = mean(abs(error)),
    bias = mean(error),
    r2 = ifelse(ss_total > 0, 1 - sum(error^2) / ss_total, NA_real_)
  )
}
validation <- rbindlist(validation_rows)
validation_summary <- validation[, lapply(.SD, mean), .SDcols = c("rmse", "mae", "bias", "r2")]
validation_sd <- validation[, lapply(.SD, sd), .SDcols = c("rmse", "mae", "bias", "r2")]
setnames(validation_sd, names(validation_sd), paste0(names(validation_sd), "_sd"))
fwrite(validation, file.path(output_dir, "ranger_top10_mean_validation_repeats.csv"))
fwrite(cbind(validation_summary, validation_sd), file.path(output_dir, "ranger_top10_mean_validation_summary.csv"))

ranking_plot <- ggplot(ranking, aes(rank, oob_rmse_mean)) +
  geom_line(color = "#2c7fb8") +
  geom_point(aes(color = rank <= 10), size = 1.5) +
  scale_color_manual(values = c("FALSE" = "#2c7fb8", "TRUE" = "#d95f0e"), guide = "none") +
  labs(x = "候选参数排名", y = "重复 OOB RMSE 均值 (m)", title = "ranger 超参数 OOB 排名") +
  theme_bw(base_size = 11)
ggsave(file.path(figure_dir, "ranger_oob_ranking.png"), ranking_plot, width = 7, height = 4.5, dpi = 300)

sensitivity_plot <- ggplot(ranking, aes(factor(num.trees), oob_rmse_mean, color = factor(mtry))) +
  geom_point() + geom_line(aes(group = interaction(mtry, sample.fraction, min.node.size)), alpha = 0.35) +
  facet_grid(min.node.size ~ sample.fraction, labeller = label_both) +
  labs(x = "树数量", y = "重复 OOB RMSE 均值 (m)", color = "mtry", title = "随机森林参数敏感性") +
  theme_bw(base_size = 10)
ggsave(file.path(figure_dir, "ranger_parameter_sensitivity.png"), sensitivity_plot, width = 10, height = 7, dpi = 300)

cat("RANGER_TUNING_OK\n")
