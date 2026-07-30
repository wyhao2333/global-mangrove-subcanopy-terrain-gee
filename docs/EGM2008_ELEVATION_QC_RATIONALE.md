# EGM2008 高程标签质控依据与引用说明

## 1. 结论

MEOW-14 分区训练默认对 `elev_median` 使用闭区间 `[-20, 50] m`。这里的高程是由 GEDI `elev_lowestmode` 聚合值从 WGS84 椭球高改正后的 **EGM2008 正高**。该规则是一个保守的**标签完整性筛选**，用于排除极少数明显不适合成为林下地形训练标签的值；它不是、也不能被表述为“全球红树林只在 -20 至 50 m 高程分布”的生态学定律。

建议在论文方法中使用如下表述：

> 为减少异常的空间激光高程标签对回归模型的影响，本研究在统一至 EGM2008 正高后，对 10 m 聚合 GEDI 标签实施保守的完整性筛选（`-20 <= H <= 50 m`）。该范围并非红树林栖息地的普适生态高程界限，而是结合潮间带、低平地形背景与本研究全球样本分布确定的宽容异常值规则。阈值边界值保留；全部剔除记录及其区域分布均单独审计。

## 2. 本项目数据诊断

转换后的完整训练表共有 `7,010,871` 个 10 m 聚合像元，且不改变原始 GEDI 值、AlphaEarth 特征、行顺序或像元坐标。EGM2008 转换诊断得到：

| 指标 | 数值 |
| --- | ---: |
| 中位数 | 2.80 m |
| 99.9% 分位数 | 29.59 m |
| `< -20 m` | 5,358 条 |
| `> 50 m` | 2,232 条 |
| 默认剔除合计 | 7,590 条（0.108%） |
| 默认保留 | 7,003,281 条（99.892%） |

因此，`50 m` 比本样本的 99.9% 分位数高约 20 m；`-20 m` 则向海平面以下提供了同样很宽的容差。该规则没有围绕中位数或主体样本作紧密裁剪，也不会影响绝大多数训练像元。它的目的仅是隔离两个极端尾部，避免错误的最低模式、残余冠层/水面回波、时间或几何不一致、以及局地垂直基准问题进入标签。

阈值是闭区间：`-20.0 m` 和 `50.0 m` 本身保留；只有严格小于下限或严格大于上限的记录会被去除。运行 `run_06b_prepare_meow14_training.bat` 后，可在以下文件审计实际影响：

```text
outputs/training/meow14_egm2008_qc_v001/elevation_qc_audit.json
outputs/training/meow14_egm2008_qc_v001/elevation_qc_by_region.csv
```

## 3. 为什么采用一个宽容的范围

### 3.1 生态与地貌背景支持“低平、受潮控制”，但不支持全球统一绝对阈值

红树林的建立、存活和群落组成受潮汐淹水、水文与相对高程控制；它们通常位于沿海、河口和潮间带的低平环境。Krauss et al. (2008) 将水文和高程列为红树林建立与早期发育的重要环境驱动；Lovelock et al. (2015) 则从全球多区域资料说明了红树林对相对海平面变化的敏感性。Wang et al. (2026) 的新西兰红树林林下地形方案也明确指出，红树林区通常“low and relatively flat”，但同时存在空间差异。

这些文献支持将远离潮间带/低平环境的极端值视为需要审查的标签，而**不支持**把某一个数值当作所有区域、所有潮差和所有垂直基准下的严格生态边界。特别是 EGM2008 正高不是地方平均海平面、平均高潮位或植被耐淹阈值；不能将它们混为一谈。

### 3.2 方法学背景支持“质量筛选”，但不支持只凭高程判断地面点

空间激光在密集红树林冠层下的地面回波稀疏，并会受潮位、噪声和植被结构影响。Wang et al. (2026) 对 ICESat-2 使用信噪比、坡度、云条件、光子分布等多重物理与几何约束提取地面控制点；Huang et al. (2024) 同样提出多准则筛选林下高程控制点。这说明仅依靠一个高度范围不能证明单个 GEDI 足迹是“真实地面”，但在已有质量标志筛选之后，宽容的高程完整性规则可以作为防止极端错误标签进入机器学习的最后一道审计措施。

本项目已先对 GEDI 月度足迹执行 `quality_flag == 1`、`degrade_flag == 0` 和有效 `elev_lowestmode` 筛选。`[-20, 50] m` 只在 GMW/MEOW 空间归属成功后施加于 10 m 中值标签；它不替代 GEDI 质量标志，也不替代未来基于 LiDAR/RTK 的外部验证。

### 3.3 数值选择的可复核逻辑

* **下限 -20 m：** 对潮间带红树林而言，这比接近潮位面的通常地貌位置向下留出了 20 m 的极宽余量。这样做避免因 EGM2008 与局地潮位基准不同、潮汐时相或局地河口地形而误删靠近海平面的样本；仍然低于该值的记录应优先检查其 GEDI 最低模式、坐标与水面/非地面回波。
* **上限 50 m：** 该值明显高于本项目 99.9% 分位数（29.59 m），保留了高于主体分布约 20 m 的缓冲。超过该值的少量像元在潮间带低平背景下更像是异常训练标签，而不是需要用来外推的典型林下地形。
* **双侧而非单侧：** 只去除高值不能防止深负值将模型向不合理方向拉动；只去除负值又无法隔离可能残留的冠层或模式识别异常。因此同时审计上下两个尾部。
* **可更改但须重跑：** 阈值位于 `config.yaml` 的 `regional_modeling.elevation_min_m` 与 `elevation_max_m`。任何改变都会改变每区的 train/test 样本，必须重新运行步骤 6b、6c 和 6d；不能与当前 `egm2008_qc_v001` 结果混用。

## 4. 推荐引用

下列文献分别支持“潮间带/低平背景”和“林下激光地面点应多约束筛选”。没有任何一篇被用来宣称全球红树林存在统一的 `-20 m` 或 `50 m` 生态上限。

1. Krauss, K. W., Lovelock, C. E., McKee, K. L., López-Hoffman, L., Ewe, S. M. L. & Sousa, W. P. Environmental drivers in mangrove establishment and early development: A review. *Aquatic Botany* **89**, 105-127 (2008). https://doi.org/10.1016/j.aquabot.2007.12.014
2. Lovelock, C. E. *et al.* The vulnerability of Indo-Pacific mangrove forests to sea-level rise. *Nature* **526**, 559-563 (2015). https://doi.org/10.1038/nature15538
3. Fatoyinbo, T. E., Simard, M., Washington-Allen, R. A. & Shugart, H. H. Landscape-scale extent, height, biomass, and carbon estimation of Mozambique's mangrove forests with Landsat ETM+ and Shuttle Radar Topography Mission elevation data. *Journal of Geophysical Research: Biogeosciences* (2008). https://doi.org/10.1029/2007JG000551
4. Payo, A. *et al.* Projected changes in area of the Sundarban mangrove forest in Bangladesh due to SLR by 2100. *Climatic Change* (2016). https://doi.org/10.1007/s10584-016-1769-z. 该文可作为低海拔、海平面变化敏感性的区域性例证，不应用其局地条件外推为全球阈值。
5. Huang, J., Wang, Y. & Yu, Y. Multi-Criteria Filtration and Extraction Strategy for Understory Elevation Control Points Using ICESat-2 ATL08 Product. *Forests* **15**, 2064 (2024). https://doi.org/10.3390/f15122064
6. Wang, Y. *et al.* RFDTM: A national-scale and wall-to-wall 30 m resolution mangrove sub-canopy topography dataset for New Zealand derived from ICESat-2 ATLAS and multi-band SAR. *Earth System Science Data Discussions* preprint (2026). https://doi.org/10.5194/essd-2026-356

## 5. 审稿与结果表述边界

* 可表述：该范围是基于潮间带低平背景、已转换 EGM2008 标签的经验尾部诊断和错误标签防护而设定的保守完整性筛选。
* 不可表述：`[-20, 50] m` 是全球红树林的真实生态高程范围，或被任一单篇文献直接规定。
* 可报告：筛选前后样本数、各区剔除比例、阈值敏感性实验，以及在独立 LiDAR/RTK 数据上的精度变化。
* 不可混淆：GEDI 聚合标签的随机内部 test30 指标不是独立地形精度；EGM2008 正高也不是局地潮位基准。

在获得独立 LiDAR/RTK 后，应统一垂直基准和潮位参考，并额外比较“无范围筛选”、“`[-20,50] m`”与更严格备选范围的外部验证结果。届时可用外部误差而非仅分布诊断确定最终产品阈值。
