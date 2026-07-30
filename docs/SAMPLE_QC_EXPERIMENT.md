# GEDI 红树林林下地形样本筛选试验

## 1. 目的和结论边界

本试验针对 `data/mangrove_gedi_alphaearth_training_egm2008.parquet` 的 10 m 聚合像元，比较三类候选标签规则：基础质量控制、绝对高程范围候选和局地空间一致性候选。原始 EGM2008 Parquet 不会被修改、删除或重新聚合。

在没有独立 LiDAR、RTK 或统一潮位基准参考数据的前提下，本流程只能比较：

1. GEDI 聚合标签的重复观测稳定性代理；
2. 标签与同一 MEOW 区内局地邻域的一致性；
3. AlphaEarth 64 维特征对 GEDI 聚合标签的内部可预测性。

因此，R 的 RMSE、MAE、Bias 和 R2 只能称为“对 GEDI 聚合标签的随机内部对照”，不是全球红树林林下真实地形精度。任何候选规则都不能仅凭本试验升级为生产硬筛选规则。

## 1.1 MEOW-14 空间归属审计

样本归属严格采用现有 14 个 MEOW 派生建模区。源 Shapefile 含自相交环，因此程序先以 Shapely/GEOS 的 `buffer(0)` 修复几何，再验证所有区域有效且任意两区没有面积重叠；只有 `buffer(0)` 无法修复时才回退到 `make_valid()`。这是一个明确记录的几何修复，不是按距离或坐标把样本任意归入相邻区域。

每个像元必须恰好命中一个区域。任一未归属或多重归属都会终止流程，并在 `qc_region_assignment_failure_samples.csv` 中保留最多 500 个坐标示例。当前随项目提供的区域面经 `buffer(0)` 修复后，对完整 7,010,871 个 EGM2008 像元复查的未归属数与多重归属数均为 0。

## 2. 三层样本定义

| 样本层 | 规则 | 用途 |
| --- | --- | --- |
| `base_qa` | 已在上游执行 `quality_flag == 1`、`degrade_flag == 0`、有效 `elev_lowestmode` 和有限 EGM2008 转换 | 当前可用的主样本基线 |
| `range_candidate` | `-20 <= elev_median <= 50 m`，边界保留 | 高程范围敏感性对照，不是生态绝对边界 |
| `spatial_consistency_candidate` | 不是被 H3 局地离群规则标记的像元 | 局地一致性敏感性对照 |
| `provisional_screened` | 同时满足 `range_candidate` 且非局地候选离群 | 多证据候选集，仅用于比较 |
| `repeat_high_confidence` | `elev_count >= 2` 且 `elev_iqr <= 1/2/5 m` | 小规模标签稳定性代理参考集，不作为全球主训练集 |

`elev_count == 1` 时的 `elev_iqr` 可能为零或无定义，它不代表观测稳定；程序明确要求 `elev_count >= 2` 才会把像元标记为 `repeat_high_confidence`。

## 3. 局地空间一致性标记

每个像元按经纬度映射到 H3 八级六边形，并在同一 `REG_CODE + H3` 单元内计算：

- 局地中位数 `m`；
- 局地 MAD，即 `median(abs(elev_median - m))`；
- 绝对残差 `abs(elev_median - m)`；
- 稳健 Z 分数：`abs(elev_median - m) / [1.4826 * max(MAD, 0.5 m)]`。

只有单元内至少有 5 个样本，且一个样本同时满足绝对残差大于 5 m、稳健 Z 大于 6 时，才标记为 `spatial_outlier_flag`。该标记是“候选异常”，不是对真实地形的裁决。H3 尺度、MAD 下限和阈值全部在 `sample_qc_experiment` 中可配置，并写入审计 JSON。

## 4. 为什么 `[-20, 50] m` 只是候选对照

红树林通常位于潮间带、河口和低平海岸，其分布受相对高程和水文过程控制；这可支持对极端标签开展审查，但不支持以单一 EGM2008 正高范围定义所有全球红树林的生态边界。EGM2008 正高也不等同于任何地点的平均海平面、平均高潮位或局地潮位基准。

此前 EGM2008 诊断中发现的极端尾部值得审查，但“尾部比例很低”不是阈值正确性的证据。本项目将 `[-20, 50] m` 仅作为范围候选，与不做范围筛选的 `base_qa` 和多证据 `provisional_screened` 并列比较；默认 `regional_modeling.elevation_qc_enabled` 已设为 `false`。

若一个候选规则使任一 MEOW 区样本留存率低于基线 50%，或者在重复观测代理集和内部对照中没有呈现一致收益，则不建议将其升级为全球硬筛选。

## 5. GEDI 原始质量字段 pilot

现有 AlphaEarth 采样表只含 `quality_flag`、`degrade_flag` 和 `elev_lowestmode`。在全量补提取任何新质量字段前，流程先在每个 MEOW 区选择一个稳定的 H3 八级小块：

1. `inspect-gedi-qc-bands` 只读取 GEDI 月度集合的 `bandNames()`；
2. `export-gedi-qc-pilot` 默认只预览 14 个空间块；
3. 仅在显式增加 `--submit` 后，导出实际存在的 `sensitivity`、`elev_sensitivity`、`surface_flag`、`num_detectedmodes`、`beam`、`solar_elevation` 等字段；
4. pilot 不调用 AlphaEarth，也不启动全量 GEDI 重导。

字段存在不等于可直接作为地面可靠性阈值。应结合 GEDI L2A 数据字典、字段分布、区域留存偏差和后续独立验证决定其是否有资格进入全量质量规则。

## 6. 固定参数内部对照

为了不把超参数优化误判为筛选收益，三个候选模型均使用 64 个 AlphaEarth 波段、冻结的 `sample_id` 和 seed=42 的 70/30 划分，以及相同的 `ranger` 参数。每个区域的候选训练集采用相同上限，且只保留 64 个特征均为有限值的像元。

评估包含：

- 固定的 `base_qa test30`；
- 未参加训练、同时满足多重访和 IQR 阈值的 `repeat_high_confidence test30` 代理集；
- RMSE、MAE、Bias（预测值减观测值）、R2、残差分位数；
- 各 MEOW 区留存率、H3 覆盖和高程分布变化。

该设计用于回答“候选筛选是否让 GEDI 标签更稳定、更局地一致或更易于由 AlphaEarth 表征”，不用于声称哪个候选集有最高真实地形精度。

## 7. 可复现操作顺序

1. 双击 `run_05c_prepare_sample_qc.bat`：生成 `outputs/analysis/sample_qc_experiment/qc_flags.parquet`、分区审计和图件。
2. 双击 `run_05d_inspect_gedi_qc_bands.bat`：只读确认 GEDI 可用字段。
3. 双击 `run_05e_preview_gedi_qc_pilot.bat`：预览 14 个小块；确认后才双击 `run_05f_submit_gedi_qc_pilot.bat`。
4. 双击 `run_06g_prepare_sample_qc_model_inputs.bat`：生成候选 R 输入 CSV。
5. 先运行 `run_06a_check_r.bat`，再双击 `run_06h_evaluate_sample_qc_candidates.bat`。
6. 双击 `run_06i_create_sample_qc_report.bat`：生成中文 Word 报告。

所有生成的数据、临时 GEE Assets、日志和图件均由 `.gitignore` 排除，不提交到公开仓库。

## 8. 文献依据和可外推边界

1. Quirós, E., Polo, M. E. & Fragoso-Campón, L. GEDI Elevation Accuracy Assessment: A Case Study of Southwest Spain. *IEEE Journal of Selected Topics in Applied Earth Observations and Remote Sensing* **14** (2021). doi:10.1109/JSTARS.2021.3080711. 该研究支持将 GEDI 高程精度单独评估；其局地结论不应直接转成全球红树林阈值。
2. Huang, J., Wang, Y. & Yu, Y. Multi-Criteria Filtration and Extraction Strategy for Understory Elevation Control Points Using ICESat-2 ATL08 Product. *Forests* **15**, 2064 (2024). doi:10.3390/f15122064. 支持林下控制点采用多准则过滤和独立验证，不支持将 ATL08 局地规则直接迁移为 GEDI 全球阈值。
3. Pronk, M., Eleveld, M. A. & Ledoux, H. Assessing Vertical Accuracy and Spatial Coverage of ICESat-2 and GEDI Spaceborne Lidar for Creating Global Terrain Models. *Remote Sensing* **16**, 2259 (2024). doi:10.3390/rs16132259. 支持使用独立高精度参考评价星载激光地形，而非以内部拟合代替外部精度。
4. Zhu, X. et al. Evaluation and Comparison of ICESat-2 and GEDI Data for Terrain and Canopy Height Retrievals in Short-Stature Vegetation. *Remote Sensing* **15**, 4969 (2023). doi:10.3390/rs15204969. 支持把植被结构、地形和观测条件作为误差背景因素；研究对象不同，不能外推为全球红树林数值阈值。
5. Wang, Y. et al. RFDTM: A national-scale and wall-to-wall 30 m resolution mangrove sub-canopy topography dataset for New Zealand derived from ICESat-2 ATLAS and multi-band SAR. *Earth System Science Data Discussions* preprint (2026). doi:10.5194/essd-2026-356. 支持在红树林林下地形制图中使用多重信号、几何和空间约束；其新西兰 ICESat-2/SAR 流程不定义全球 GEDI 的绝对高程范围。
