# MEOW-14 红树林林下地形：逐区 ranger 重复 OOB 调参。
# 只读取各区固定 train70；test30 不参与调参或参数选择。

args <- commandArgs(trailingOnly = TRUE)
arg_value <- function(name, default = NULL) {
  index <- match(name, args)
  if (is.na(index) || index == length(args)) return(default)
  args[[index + 1]]
}

manifest_path <- arg_value("--manifest")
output_dir <- arg_value("--output-dir")
run_mode <- arg_value("--run-mode", "full")
repeats <- as.integer(arg_value("--repeats", "5"))
fraction <- as.numeric(arg_value("--fraction", "0.10"))
seed <- as.integer(arg_value("--seed", "42"))
requested_regions <- arg_value("--regions", "")
as_num_list <- function(value, default) {
  if (is.null(value) || !nzchar(value)) return(default)
  parsed <- suppressWarnings(as.numeric(trimws(strsplit(value, ",", fixed = TRUE)[[1]])))
  if (any(!is.finite(parsed))) stop("参数网格包含非数字值。")
  parsed
}
grid_trees <- as.integer(as_num_list(arg_value("--trees", ""), c(100, 200, 300)))
grid_mtry <- as.integer(as_num_list(arg_value("--mtry", ""), c(8, 16)))
grid_bag <- as_num_list(arg_value("--bag-fractions", ""), c(0.5, 0.632))
grid_min_node <- as.integer(as_num_list(arg_value("--min-node-sizes", ""), c(5, 10)))
if (is.null(manifest_path) || is.null(output_dir)) stop("必须提供 --manifest 和 --output-dir。")
if (!is.finite(fraction) || fraction <= 0 || fraction > 1) stop("--fraction 必须在 (0, 1] 内。")

required_packages <- c("ranger", "data.table", "ggplot2")
missing_packages <- required_packages[!vapply(required_packages, requireNamespace, logical(1), quietly = TRUE)]
if (length(missing_packages) > 0) stop(paste("缺少 R 包：", paste(missing_packages, collapse = ", ")))
library(ranger)
library(data.table)
library(ggplot2)

predictors <- sprintf("A%02d", 0:63)
target <- "elev_median"
needed <- c(predictors, target)
manifest <- fread(manifest_path)
if (!all(c("region_code", "train_csv") %in% names(manifest))) stop("区域清单缺少 region_code 或 train_csv 字段。")
if (nrow(manifest) != 14) stop(sprintf("区域清单应为 14 个区域，当前为 %d 个。", nrow(manifest)))
if (nzchar(requested_regions)) {
  requested <- trimws(strsplit(requested_regions, ",", fixed = TRUE)[[1]])
  manifest <- manifest[region_code %in% requested]
  if (nrow(manifest) == 0) stop("--regions 未匹配任何区域代码。")
}

grid <- expand.grid(
  num.trees = grid_trees,
  mtry = grid_mtry,
  sample.fraction = grid_bag,
  min.node.size = grid_min_node,
  KEEP.OUT.ATTRS = FALSE,
  stringsAsFactors = FALSE
)
grid$candidate_id <- seq_len(nrow(grid))
if (nrow(grid) != 24) stop("参数网格必须为 24 组；请检查 regional_modeling.tuning_grid_* 配置。")
if (run_mode == "quick") {
  grid <- head(grid, 2)
  repeats <- 1L
}
dir.create(output_dir, recursive = TRUE, showWarnings = FALSE)
fwrite(as.data.table(grid), file.path(output_dir, "regional_ranger_hypergrid.csv"))
formula <- as.formula(paste(target, "~", paste(predictors, collapse = " + ")))
threads <- max(1L, parallel::detectCores() - 1L)
region_seed <- function(code) seed + sum(utf8ToInt(as.character(code))) * 1009L

summary_rows <- list()
summary_index <- 1L
for (row_index in seq_len(nrow(manifest))) {
  item <- manifest[row_index]
  code <- as.character(item$region_code)
  train_path <- as.character(item$train_csv)
  if (!file.exists(train_path)) stop(sprintf("区域 %s 找不到 train CSV：%s", code, train_path))
  region_dir <- file.path(output_dir, code)
  dir.create(region_dir, recursive = TRUE, showWarnings = FALSE)
  message(sprintf("\n========== 区域 %s：读取完整 train70 ==========" , code))
  train <- fread(train_path, select = needed, showProgress = FALSE)
  train <- train[complete.cases(train[, ..needed])]
  if (nrow(train) < 2) stop(sprintf("区域 %s 的有效 train70 样本不足。", code))
  sample_size <- max(1L, floor(nrow(train) * fraction))
  if (sample_size < 2) stop(sprintf("区域 %s 的 10%% 调参子样本不足 2 条。", code))
  message(sprintf("区域 %s：完整 train70=%s；每次无放回子样本=%s；重复=%d；候选=%d", code,
                  format(nrow(train), big.mark = ","), format(sample_size, big.mark = ","), repeats, nrow(grid)))
  result_rows <- list()
  result_index <- 1L
  samples <- vector("list", repeats)
  for (repeat_id in seq_len(repeats)) {
    set.seed(region_seed(code) + repeat_id)
    # 每次样本只抽取一次，并被该重复的全部参数候选共享。
    samples[[repeat_id]] <- train[sample.int(nrow(train), sample_size, replace = FALSE)]
    for (grid_index in seq_len(nrow(grid))) {
      params <- grid[grid_index, ]
      message(sprintf("区域 %s | 重复 %d/%d | 参数 %d/%d | trees=%d mtry=%d bag=%.3f minnode=%d", code,
                      repeat_id, repeats, grid_index, nrow(grid), params$num.trees, params$mtry,
                      params$sample.fraction, params$min.node.size))
      fit <- ranger(
        formula = formula,
        data = samples[[repeat_id]],
        num.trees = params$num.trees,
        mtry = params$mtry,
        sample.fraction = params$sample.fraction,
        min.node.size = params$min.node.size,
        replace = FALSE,
        seed = region_seed(code) + repeat_id,
        num.threads = threads,
        importance = "none"
      )
      result_rows[[result_index]] <- data.table(
        region_code = code,
        candidate_id = params$candidate_id,
        repeat_id = repeat_id,
        num.trees = params$num.trees,
        mtry = params$mtry,
        sample.fraction = params$sample.fraction,
        min.node.size = params$min.node.size,
        oob_rmse = sqrt(fit$prediction.error)
      )
      result_index <- result_index + 1L
    }
  }
  results <- rbindlist(result_rows)
  fwrite(results, file.path(region_dir, "ranger_repeated_oob_results.csv"))
  ranking <- results[, .(
    oob_rmse_mean = mean(oob_rmse),
    oob_rmse_sd = if (.N > 1) sd(oob_rmse) else 0,
    repeats = .N
  ), by = .(region_code, candidate_id, num.trees, mtry, sample.fraction, min.node.size)][order(oob_rmse_mean, oob_rmse_sd)]
  ranking[, rank := seq_len(.N)]
  fwrite(ranking, file.path(region_dir, "ranger_oob_ranking.csv"))
  top_n <- min(10L, nrow(ranking))
  top <- head(ranking, top_n)
  fwrite(top, file.path(region_dir, "ranger_top10_params.csv"))
  gee_params <- data.table(
    region_code = code,
    top_n = top_n,
    numberOfTrees = round(mean(top$num.trees)),
    variablesPerSplit = round(mean(top$mtry)),
    bagFraction = round(mean(top$sample.fraction), 3),
    minLeafPopulation = round(mean(top$min.node.size)),
    seed = region_seed(code),
    selection_metric = "repeated_oob_rmse_mean",
    replace = FALSE
  )
  fwrite(gee_params, file.path(region_dir, "ranger_top10_mean_params_for_gee.csv"))
  confirmation <- list()
  for (repeat_id in seq_len(repeats)) {
    fit <- ranger(
      formula = formula,
      data = samples[[repeat_id]],
      num.trees = gee_params$numberOfTrees,
      mtry = gee_params$variablesPerSplit,
      sample.fraction = gee_params$bagFraction,
      min.node.size = gee_params$minLeafPopulation,
      replace = FALSE,
      seed = region_seed(code) + repeat_id,
      num.threads = threads,
      importance = "none"
    )
    confirmation[[repeat_id]] <- data.table(region_code = code, repeat_id = repeat_id, oob_rmse = sqrt(fit$prediction.error))
  }
  confirmation <- rbindlist(confirmation)
  fwrite(confirmation, file.path(region_dir, "ranger_top10_mean_confirmation_oob.csv"))
  summary_rows[[summary_index]] <- cbind(
    gee_params,
    data.table(
      train_rows = nrow(train),
      tuning_rows_per_repeat = sample_size,
      best_oob_rmse = ranking$oob_rmse_mean[[1]],
      confirmation_oob_rmse_mean = mean(confirmation$oob_rmse),
      confirmation_oob_rmse_sd = if (nrow(confirmation) > 1) sd(confirmation$oob_rmse) else 0
    )
  )
  summary_index <- summary_index + 1L
  ranking_plot <- ggplot(ranking, aes(rank, oob_rmse_mean)) +
    geom_line(color = "#2c7fb8") +
    geom_point(aes(color = rank <= top_n), size = 1.6) +
    scale_color_manual(values = c("FALSE" = "#2c7fb8", "TRUE" = "#d95f0e"), guide = "none") +
    labs(x = "参数候选排名", y = "重复 OOB RMSE (m)", title = paste0("MEOW-14 ", code, " ranger 参数排名")) +
    theme_bw(base_size = 11)
  ggsave(file.path(region_dir, "ranger_oob_ranking.png"), ranking_plot, width = 7, height = 4.5, dpi = 240)
}
summary <- rbindlist(summary_rows)
fwrite(summary, file.path(output_dir, "regional_tuning_summary.csv"))
cat("REGIONAL_RANGER_TUNING_OK\n")
