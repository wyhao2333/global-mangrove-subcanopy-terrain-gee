# 全球红树林林下地形 GEE 数据制备流程

这个项目用于生成全球红树林林下地形建模所需的训练表：

```text
GMW 2020 红树林范围
  -> GEDI 25 m 月度脚印高程 elev_lowestmode
  -> AlphaEarth / Satellite Embedding 10 m 64 维特征
  -> 本地按 10 m embedding 像元做 GEDI 高程中值聚合
```

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

至少需要这 4 个文件：

```text
data/raw/gmw_v3/gmw_v3_2020_vec.shp
data/raw/gmw_v3/gmw_v3_2020_vec.shx
data/raw/gmw_v3/gmw_v3_2020_vec.dbf
data/raw/gmw_v3/gmw_v3_2020_vec.prj
```

这些文件很大，不会提交到 GitHub。

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
- 把全球红树林碎面切成很多小 GeoJSON shards。
- 每个 shard 控制在 0.8 MB 左右，避免 GEE 请求过大。

大概多久：

- 全量 GMW 可能需要几十分钟到数小时。

成功后会生成：

```text
data/index/gmw_bounds_index.csv
data/index/gmw_1deg_cells.csv
data/index/gmw_6deg_tiles.csv
data/index/aoi_shards.csv
data/shards/*.geojson
```

可以重复运行吗：

- 可以。会覆盖同名索引和 shard。

如果你只想快速测试，不想全量切分，可以用 PowerShell 方式运行小样本命令，见后文。

### 5. 双击 `run_03_sample_smoke_test.bat`

作用：

- 只用 1 个 shard。
- 只跑 2020 年。
- 把 GEDI + AlphaEarth 小样本直接下载到本地。

成功后会生成：

```text
outputs/raw_samples/*.parquet
logs/sample_tasks_*.csv
```

大概多久：

- 通常几分钟到几十分钟。
- 如果 GEE 忙，可能更久。

可以重复运行吗：

- 可以。

### 6. 双击 `run_04_sample_all.bat`

作用：

- 正式提交全量采样任务。
- 默认按 shard + year 分批提交。
- 默认导出到 Google Drive，而不是一次性拉回本地。

为什么全量默认导出到 Google Drive：

- 全量可能有几百万到一千多万条记录。
- 直接本地 `getInfo` 很容易超时或失败。
- Google Drive table export 更稳。

成功后会生成任务登记表：

```text
logs/sample_tasks_YYYYMMDD_HHMMSS.csv
```

请到 Google Drive 中的文件夹下载 CSV：

```text
mangrove_gedi_alphaearth_samples
```

下载后放到：

```text
outputs/raw_samples/
```

默认每次最多提交 `20` 个新任务，避免一下子提交太多。  
如果想改，在 `config.yaml` 里改：

```yaml
sampling:
  max_new_tasks: 20
```

可以重复运行吗：

- 可以，但要注意不要重复提交同一批 shard/year。
- 每次提交情况会记录在 `logs/sample_tasks_*.csv`。

### 7. 双击 `run_05_aggregate.bat`

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

### 小样本切分 GMW

只生成 5 个 shards，适合测试：

```powershell
.\.venv\Scripts\python.exe -m mangrove_terrain --config config.yaml prepare-gmw --max-shards 5
```

### 全量切分 GMW

```powershell
.\.venv\Scripts\python.exe -m mangrove_terrain --config config.yaml prepare-gmw --all
```

### 跑小样本采样

```powershell
.\.venv\Scripts\python.exe -m mangrove_terrain --config config.yaml sample --smoke
```

### 提交全量采样任务

```powershell
.\.venv\Scripts\python.exe -m mangrove_terrain --config config.yaml sample --mode drive --years 2019-2025
```

### 本地聚合

```powershell
.\.venv\Scripts\python.exe -m mangrove_terrain --config config.yaml aggregate
```

## 数据字段说明

原始采样表字段：

```text
shard_id          GMW shard 编号
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

因此代码按每张 GEDI 月度影像分别采样，然后合并表格。

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
已经生成的 shard、日志、下载的表都不会自动删除。  
重新运行时请查看 `logs/sample_tasks_*.csv`，避免重复提交同一个 shard/year。

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

