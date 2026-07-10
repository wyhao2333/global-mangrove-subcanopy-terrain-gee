# 全球红树林林下地形 GEE 数据制备流程

这个项目用于生成全球红树林林下地形建模所需的训练表：

```text
GMW 2020 红树林范围
  -> GEDI 25 m 月度脚印高程 elev_lowestmode
  -> AlphaEarth / Satellite Embedding 10 m 64 维特征
  -> 本地按 10 m embedding 像元做 GEDI 高程中值聚合
```

正式流程采用“两阶段”：阶段1只提取并保存全部GEDI月度脚印为GEE表资产；阶段2从表资产按空间块采样AlphaEarth。这样可以避免把几十个月的GEDI点和全球AlphaEarth均值一次性放进同一张计算图。

推荐流程按 GEDI 原生 6°瓦片运行，全球 GMW 范围约 201 个瓦片。GMW 本地 shp 用来生成瓦片索引和一致性验证；GEE 计算时使用同版本的 GMW v3 2020 公共栅格掩膜，避免反复计算一百多万个复杂矢量面。

项目已核验这 201 个瓦片在 GEE 中全部有对应 GEDI 数据，2019-2025 合计 12,688 张月度瓦片影像，每个空间瓦片包含 55-64 个月；内部仍逐月提取，不做时间合成。

本项目只做数据制备。机器学习建模和调参暂时不做，后续可以接 Python、R 或 GEE 模型。

## 重要提醒

请不要自己运行：

```text
pip install geopandas
pip install gdal
pip install geemap
```

Windows 上这些库很容易因为 GDAL、PROJ、GEOS、Fiona 版本不一致而安装失败。  
本项目默认 **不依赖 GeoPandas、不依赖 geemap、不依赖 Python GDAL 包**，只使用更容易安装的 wheel 包：

```text
pyogrio + pyarrow + shapely + pyproj
```

## 电脑需要先安装什么

1. Windows 10 或 Windows 11。
2. Python 3.11。
3. 一个能访问 Google Earth Engine 的 Google 账号。
4. 一个 GEE / Google Cloud project，例如 `ee-wyhao00203` 或你自己的 project。

如果你不知道有没有 Python：

1. 打开项目文件夹。
2. 在文件夹空白处按住 `Shift`，点鼠标右键。
3. 点“在终端中打开”。
4. 输入：

```powershell
python --version
```

如果能看到类似 `Python 3.11.x`，说明 Python 可以用。

## 第 0 步：准备 GMW 数据

请把 GMW 2020 矢量文件放到：

```text
data/raw/gmw_v3/
```

也就是说，放完之后项目文件夹里应该能看到：

```text
global-mangrove-subcanopy-terrain-gee/
  data/
    raw/
      gmw_v3/
        gmw_v3_2020_vec.shp
        gmw_v3_2020_vec.shx
        gmw_v3_2020_vec.dbf
        gmw_v3_2020_vec.prj
```

至少需要这 4 个文件：

```text
data/raw/gmw_v3/gmw_v3_2020_vec.shp
data/raw/gmw_v3/gmw_v3_2020_vec.shx
data/raw/gmw_v3/gmw_v3_2020_vec.dbf
data/raw/gmw_v3/gmw_v3_2020_vec.prj
```

这些文件很大，不会提交到 GitHub。

GEE 采样默认读取公共资产：

```text
projects/earthengine-legacy/assets/projects/sat-io/open-datasets/GMW/extent/GMW_V3
```

使用其中的 `gmw_v3_2020` 栅格。该资产来自 [GMW v3 社区目录](https://gee-community-catalog.org/projects/mangrove/)，与本地 GMW 2020 shp 属于同一数据版本；如果社区资产将来迁移，可以在 `config.yaml` 中替换为自己的 GMW 栅格资产。

## 最简单运行方式：双击运行

如果你完全不会 PowerShell，按下面顺序双击文件即可。

### 1. 双击 `setup_windows.bat`

作用：

- 创建 `.venv` Python 环境。
- 安装依赖。
- 如果没有 `config.yaml`，自动从 `config.example.yaml` 复制一份。

大概多久：

- 第一次通常需要 3 到 15 分钟。
- 取决于网络和电脑速度。

成功标志：

- 窗口最后显示 `Setup finished.`
- 项目文件夹里出现 `.venv` 文件夹。
- 项目文件夹里出现 `config.yaml`。

失败怎么办：

- 如果提示找不到 Python，请先安装 Python 3.11。
- 如果提示某个包没有 wheel，请把错误截图发给维护者。

可以重复运行吗：

- 可以。重复运行不会删除已有数据。

### 2. 修改 `config.yaml`

用记事本打开 `config.yaml`，重点检查：

```yaml
gee:
  project: ee-wyhao00203

paths:
  gmw_shp: data/raw/gmw_v3/gmw_v3_2020_vec.shp
```

如果你要换 GEE project，就改 `project`。

### 3. 双击 `run_01_check_gee.bat`

作用：

- 检查 GEE 登录。
- 检查 GEDI 数据集是否能访问。
- 检查 AlphaEarth / Satellite Embedding 数据集是否能访问。

第一次运行时会弹出浏览器：

1. 选择你的 Google 账号。
2. 同意 Earth Engine 权限。
3. 浏览器显示认证完成后，回到黑色窗口。
4. 等待程序继续。

成功标志：

- 窗口里能看到 GEDI image count。
- 能看到 AlphaEarth band count 为 `64`。
- 能看到 GMW 2020 raster bands 为 `['b1']`。

常见失败：

- `Caller does not have required permission to use project`
  - 说明当前账号不能使用这个 project。
  - 解决办法：换 `config.yaml` 里的 project，或让管理员给账号加 project 权限。

可以重复运行吗：

- 可以。

### 4. 双击 `run_02_prepare_gmw.bat`

作用：

- 读取 GMW 2020 shp。
- 不用 GeoPandas。
- 生成 GEDI 原生 6°瓦片索引。
- 默认只额外生成 1 个旧式 GeoJSON shard，用于新旧流程一致性验证。
- 推荐流程不再生成约 2,200 个正式采样 shard。

大概多久：

- 需要扫描约 107 万个 GMW 要素边界，通常需要几分钟到几十分钟。

成功后会生成：

```text
data/index/gmw_bounds_index.csv
data/index/gmw_1deg_cells.csv
data/index/gmw_6deg_tiles.csv
data/index/aoi_shards.csv
data/shards/*.geojson（默认只有 1 个验证 shard）
```

可以重复运行吗：

- 可以。会覆盖同名索引和验证 shard。

旧式流程如果确实需要约 2,200 个 GeoJSON shards，可在 PowerShell 中手工运行 `prepare-gmw --all`；推荐原生瓦片流程不需要。

### 5. 双击 `run_03_sample_smoke_test.bat`

作用：

- 使用同一个小面和 2020 年 3 月有观测的数据，分别运行旧流程和新原生瓦片流程。
- 逐行比较 GEDI 影像 ID、月份、中心坐标、高程、AlphaEarth 像元坐标和 A00-A63。
- 任意脚印丢失、增加或数值改变都会直接报错，不允许进入全量导出。

注意：

- 当前真实验证结果为 57/57 行完全一致，所有数值字段最大差值为 0。
- 小面只有 57 行不代表全球样本量，只用于证明新方法没有改变采样定义。

成功后会生成：

```text
logs/native_validation_*.json
```

大概多久：

- 当前测试约 1 分钟；如果 GEE 忙，可能更久。
- 不建议在完整瓦片任务占满同一 GEE project 时并行跑验证；验证可能由约 1 分钟延长到数分钟。

可以重复运行吗：

- 可以。

### 6. 双击 `run_03b_submit_one_block_test.bat`

作用：

- 提交一个完整 GEDI 原生 6°瓦片的直接联合采样任务。
- 该命令只用于和两阶段流程做对照，或者两阶段流程的异常回退。

什么时候需要看这一步：

- 第一次换电脑、换 GEE project 或更换数据源后，建议先跑这一步。
- 双击 `run_03c_check_native_tasks.bat` 查看状态与真实耗时。

直接联合流程中，`102W_012N`完整6°瓦片耗时33.8分钟，但高密度瓦片出现计算图过大和高EECU问题，因此不再作为正式默认流程。

可以重复运行吗：

- 可以。READY、RUNNING、COMPLETED 任务会跳过；FAILED 或 CANCELLED 任务允许重新提交。

### 6.1 双击 `run_03c_check_native_tasks.bat`

检查所有原生瓦片任务状态、失败原因、EECU 和已完成任务耗时。结果写入：

```text
logs/native_task_status_latest.csv
```

### 7. 双击 `run_03e_test_staged_gedi_assets.bat`

作用：

- 提交一个完整6°瓦片的2019-2025 GEDI点表资产。
- 不计算AlphaEarth，只保留GEDI高程、质量字段、月份、影像ID和中心位置。

真实测试结果：

- `102W_012N`阶段1任务约4.1分钟完成。
- EECU约0.92秒；另一个已完成瓦片约0.64秒。

资产位置：

```text
projects/ee-wyhao00203/assets/global_mangrove_subcanopy_terrain/gedi_points/
```

### 8. 双击 `run_03f_test_staged_alpha.bat`

作用：

- 从阶段1的GEDI表资产中选取一个空间块。
- 每块默认不超过10,000个脚印。
- AlphaEarth均值先按空间块过滤，只计算覆盖该块的Embedding影像。

真实测试结果：

- `102W_012N`的2,475个脚印块约12秒完成。
- EECU约440秒。
- 对照的旧阶段2全局均值任务失败，并消耗约38,098-51,104 EECU秒。

### 9. 双击 `run_03d_check_staged_tasks.bat`

检查阶段1资产任务和阶段2 AlphaEarth任务。结果写入：

```text
logs/staged_task_status_latest.csv
```

### 10. 双击 `run_04_sample_all.bat`

作用：

- 正式提交阶段1 GEDI表资产任务。
- 程序会先询问 `Continue ... [Y/N]`；确认提交请输入 `Y`，误点或暂不提交请输入 `N`。
- 默认按约201个GEDI原生6°瓦片分批保存表资产，每个资产包含2019-2025全部月度观测。
- GEDI仍按每张月度影像分别提取，不做mosaic、不做同位置去重。
- 默认每次最多新提交 `max_new_tasks` 个任务；GEE 通常只同时运行约 3 个，其余保持 READY 排队。
- 重复运行会读取 `logs/gedi_asset_tasks_*.csv`；已经存在或正在运行的资产不会重复提交。
- 正式运行前必须完成 `run_02_prepare_gmw.bat`，生成 `gmw_6deg_tiles.csv`。

阶段1预计约201个GEE表资产，不会生成201个包含64个AlphaEarth波段的超大任务。

### 11. 双击 `run_04b_sample_alpha_all.bat`

作用：

- 等阶段1资产完成后，从资产中按空间块采样AlphaEarth。
- 默认每个空间块最多10,000个GEDI脚印。
- 每个任务只计算当前空间块覆盖的AlphaEarth影像。

阶段2输出CSV到Google Drive：

```text
mangrove_gedi_alphaearth_samples/
```

102W_012N测试瓦片共产生8个空间块，合计20,146个GEDI脚印；其他瓦片会根据点密度自动递归切分。

按此前约869万条质量合格GEDI观测估算，阶段2理论上至少需要约869个10,000点块，实际数量会因1°边界和空间密度增加。阶段1约201个表资产，在3个并行任务下预计数小时；阶段2通常是数小时到1-2天，整体建议按1-3天安排，并以 `staged_task_status_latest.csv` 的真实耗时为准。

为什么全量默认导出到 Google Drive：

- 全量可能有几百万到一千多万条记录。
- 直接本地 `getInfo` 很容易超时或失败。
- Google Drive table export 更稳。

成功后会生成任务登记表：

```text
logs/gedi_asset_tasks_YYYYMMDD_HHMMSS.csv
logs/alpha_sample_tasks_YYYYMMDD_HHMMSS.csv
```

请到 Google Drive 中的文件夹下载 CSV：

```text
mangrove_gedi_alphaearth_samples
```

下载后放到：

```text
outputs/raw_samples/
```

可以重复运行吗：

- 可以。阶段1和阶段2都会读取自己的任务日志并查询GEE状态。
- 不要删除 `logs/gedi_asset_tasks_*.csv` 和 `logs/alpha_sample_tasks_*.csv`。

默认每次最多提交 `20` 个新任务，避免一下子提交太多。  
如果想改，在 `config.yaml` 里改：

```yaml
sampling:
  max_new_tasks: 20
```

### 12. 双击 `run_05_aggregate.bat`

作用：

- 读取 `outputs/raw_samples/` 里的 CSV 或 Parquet。
- 按 AlphaEarth 10 m 像元坐标 `ae_x, ae_y` 分组。
- 对同一像元内多个 GEDI 脚印高程做中值聚合。

输出字段包括：

```text
ae_x, ae_y
lon_median, lat_median
elev_median
elev_count
elev_iqr
A00-A63
```

成功后会生成：

```text
outputs/training/mangrove_gedi_alphaearth_training.parquet
outputs/training/mangrove_gedi_alphaearth_training.preview.csv
```

其中 `.parquet` 是正式训练表，`.preview.csv` 是方便打开看的预览表。

## PowerShell 运行方式

如果你会复制命令，也可以用 PowerShell。

### 怎么打开 PowerShell

方法 1：

1. 打开项目文件夹。
2. 在空白处按住 `Shift`。
3. 鼠标右键。
4. 点“在终端中打开”。

方法 2：

1. 打开项目文件夹。
2. 点击资源管理器上方地址栏。
3. 输入：

```text
powershell
```

4. 按回车。

### 安装环境

```powershell
powershell -ExecutionPolicy Bypass -File .\setup_windows.ps1
```

### 检查 GEE

```powershell
.\.venv\Scripts\python.exe -m mangrove_terrain --config config.yaml inspect-gee
```

### 生成原生瓦片索引

```powershell
.\.venv\Scripts\python.exe -m mangrove_terrain --config config.yaml prepare-gmw --native-only
```

旧式流程需要全量 GeoJSON shards 时才运行：

```powershell
.\.venv\Scripts\python.exe -m mangrove_terrain --config config.yaml prepare-gmw --all
```

### 验证新旧采样结果一致

```powershell
.\.venv\Scripts\python.exe -m mangrove_terrain --config config.yaml validate-native --year 2020 --month 3 --limit 5000
```

### 提交一块正式 Drive 测试任务

```powershell
.\.venv\Scripts\python.exe -m mangrove_terrain --config config.yaml export-gedi-assets --tiles 102W_012N --years 2019-2025 --year-mode all
```

查看任务状态：

```powershell
.\.venv\Scripts\python.exe -m mangrove_terrain --config config.yaml check-native-tasks
```

### 提交全量采样任务

```powershell
.\.venv\Scripts\python.exe -m mangrove_terrain --config config.yaml export-gedi-assets --years 2019-2025 --year-mode all
```

阶段1资产完成后，提交阶段2 AlphaEarth空间块任务：

```powershell
.\.venv\Scripts\python.exe -m mangrove_terrain --config config.yaml sample-alpha-assets --years 2019-2025 --year-mode all
```

如果某些密集瓦片的阶段1任务失败，可以指定瓦片并按年拆分；阶段2仍然按空间块拆分：

```powershell
.\.venv\Scripts\python.exe -m mangrove_terrain --config config.yaml export-gedi-assets --tiles 018W_006N --years 2019-2025 --year-mode annual
```

### 本地聚合

```powershell
.\.venv\Scripts\python.exe -m mangrove_terrain --config config.yaml aggregate
```

## 数据字段说明

原始采样表字段：

```text
shard_id          推荐流程中为 GEDI 原生 6°瓦片编号；旧流程中为 GMW shard 编号
gedi_image_id     GEDI 月度影像 ID
year              GEDI 年份
month             GEDI 月份
lon, lat          GEDI 脚印中心位置
ae_x, ae_y        AlphaEarth 10 m 像元坐标
elev_lowestmode   GEDI 最低模式高程
quality_flag      GEDI 质量标记
degrade_flag      GEDI degrade 标记
A00-A63           AlphaEarth / Satellite Embedding 64 维特征
```

聚合训练表字段：

```text
ae_x, ae_y        AlphaEarth 10 m 像元坐标
lon_median        同一像元内 GEDI 中心经度中值
lat_median        同一像元内 GEDI 中心纬度中值
elev_median       同一像元内 GEDI elev_lowestmode 中值
elev_count        同一像元内 GEDI 脚印数量
elev_iqr          高程四分位距，用于判断离散程度
A00-A63           embedding 特征
```

## 为什么不 mosaic GEDI

本项目要保留 2019-2025 年所有 GEDI 月度观测。  
如果把 GEDI 先 mosaic，同一个位置多个月重复观测会被压成一个值，时间序列信息会丢失。

因此代码按每张 GEDI 月度影像分别提取质量合格脚印，合并全部月份后统一采样 AlphaEarth。月度观测没有合成或去重。

注意：`--year-mode all` 只是把 2019-2025 全期结果放在同一个导出任务和同一个 CSV 里；内部仍然逐张 GEDI 月度影像采样，`year`、`month`、`gedi_image_id` 都会保留。

## 常见问题

### 1. PowerShell 说不能运行脚本

请用这个命令：

```powershell
powershell -ExecutionPolicy Bypass -File .\setup_windows.ps1
```

或者直接双击 `setup_windows.bat`。

### 2. 找不到 GMW shp

检查这 4 个文件是否存在：

```text
data/raw/gmw_v3/gmw_v3_2020_vec.shp
data/raw/gmw_v3/gmw_v3_2020_vec.shx
data/raw/gmw_v3/gmw_v3_2020_vec.dbf
data/raw/gmw_v3/gmw_v3_2020_vec.prj
```

如果你放在别的位置，请修改 `config.yaml`：

```yaml
paths:
  gmw_shp: E:/your/path/gmw_v3_2020_vec.shp
```

### 3. GEE project 权限错误

如果看到：

```text
Caller does not have required permission to use project
```

说明 Google 账号没有权限使用这个 project。  
解决办法：

1. 修改 `config.yaml` 里的 `gee.project`。
2. 或者让 project 管理员给你的 Google 账号授权。

### 4. 全量采样为什么没有马上生成本地 CSV

全量采样数据太大，默认是提交 Google Drive 导出任务。  
请到 Google Drive 下载 CSV，再放到：

```text
outputs/raw_samples/
```

然后运行聚合。

### 5. 可以中断后继续吗

可以。  
已经生成的索引、验证 shard、日志和下载表都不会自动删除。
重新运行时程序会读取阶段1/阶段2任务日志并查询 GEE 状态，READY、RUNNING 和 COMPLETED 任务不会重复提交。

## GitHub 维护建议

这个仓库只提交：

- 代码
- README
- 配置示例
- 小的索引示例

不要提交：

- `.venv/`
- GMW 原始 shp/zip
- GEE 凭证
- 全量采样 CSV
- 训练表 parquet
