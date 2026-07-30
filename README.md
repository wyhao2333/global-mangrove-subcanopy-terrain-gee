# 全球红树林林下地形：GEDI + AlphaEarth MEOW-14 分区建模流程

这个项目从 GEDI / AlphaEarth 样本制备一直运行到 MEOW-14 分区模型训练：

```text
GMW 2020 红树林范围
  -> GEDI 25 m 月度脚印高程 elev_lowestmode
  -> AlphaEarth / Satellite Embedding 10 m 64 维特征
  -> 本地按 10 m embedding 像元做 GEDI 高程中值聚合
  -> PROJ 统一到 EGM2008 正高，并在独立样本 QC 试验中比较候选筛选规则
  -> MEOW-14 区域归属与固定随机 70/30 划分
  -> 本地 ranger 调参和随机内部验证
  -> GEE 保存 14 个区域回归随机森林 classifier
```

正式流程采用“两阶段加下载”：阶段1只提取并保存全部GEDI月度脚印为GEE表资产；步骤4b从表资产按空间块采样AlphaEarth并保存为当前账号的GEE表资产；步骤4c把已验证完成的表资产直接下载到本地，Google Drive 导出保留为备用方式。这避免把几十个月的GEDI点和全球AlphaEarth均值一次性放进同一张计算图，并用真实资产作为可恢复的完成标记。

推荐流程按 GEDI 原生 6°瓦片运行，全球 GMW 范围约 201 个瓦片。GMW 本地 shp 用来生成瓦片索引和一致性验证；GEE 计算时使用同版本的 GMW v3 2020 公共栅格掩膜，避免反复计算一百多万个复杂矢量面。索引按 GMW 矢量范围与格网的相交关系生成，而不是只看面中心点，因此跨 1° 或 6° 边界的红树林不会被遗漏。

项目已核验这 201 个瓦片在 GEE 中全部有对应 GEDI 数据，2019-2025 合计 12,688 张月度瓦片影像，每个空间瓦片包含 55-64 个月；内部仍逐月提取，不做时间合成。

当前唯一的生产训练流程是 **MEOW-14 分区建模**。每区独立调参、独立训练；`[-20, 50] m` 不再作为默认生产硬筛选，而是独立 QC 试验中的候选对照。生产流程目前使用既有 `base_qa` 标签，直到获得 LiDAR/RTK 外部验证后再决定是否启用额外筛选。每轮只保存区域 classifier，不启动全球 10 m 预测、区域拼接或边界羽化。

从旧版本升级时，先双击 `run_00b_sync_config.bat`：它会删除已废弃的 `modeling` 配置块，并将原有 `modeling.rscript_path` 自动迁移到 `regional_modeling.rscript_path`。

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

sampling:
  alpha_initial_batch: 30
  alpha_refill_batch: 30
  alpha_poll_minutes: 10
  alpha_active_threshold: 10
```

如果你要换 GEE project，可以改 `project`；更方便的方法见下一节，运行时通过 `--project` 临时切换，不会修改此文件。

### 2.1 新增 GEE 账号凭证（不自动打开浏览器）

双击 `run_00_add_gee_account.bat`，输入新的 GEE project ID，例如 `ee-wyhao026`。程序会在黑色窗口打印 Earth Engine 授权链接，**不会**自动打开默认浏览器。

请把链接复制到已经登录目标 Google 账号的浏览器窗口，完成授权后保持黑色窗口运行，直到显示 project 验证成功。凭证会自动保存为按 project 命名的文件：

```text
%USERPROFILE%\.config\earthengine\projects\<project-id>.json
```

例如 `ee-wyhao026` 对应：

```text
%USERPROFILE%\.config\earthengine\projects\ee-wyhao026.json
```

每个 project 使用独立凭证。后续运行程序时填入不同 project 即可切换账号，不需要手工复制、覆盖或删除凭证。若只是第一次普通运行 `run_01_check_gee.bat`，没有凭证时仍会使用原有的自动浏览器认证方式。

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
- 程序会以中文询问是否继续；输入 `Y` 确认提交，直接回车或输入其他内容取消。
- 默认按约201个GEDI原生6°瓦片分批保存表资产，每个资产包含2019-2025全部月度观测。
- GEDI仍按每张月度影像分别提取，不做mosaic、不做同位置去重。
- 默认每次最多新提交 `max_new_tasks` 个任务；GEE 通常只同时运行约 3 个，其余保持 READY 排队。
- 重复运行会读取 `logs/gedi_asset_tasks_*.csv`；已经存在或正在运行的资产不会重复提交。
- 正式运行前必须完成 `run_02_prepare_gmw.bat`，生成 `gmw_6deg_tiles.csv`。

阶段1预计约201个GEE表资产，不会生成201个包含64个AlphaEarth波段的超大任务。

### 11. 双击 `run_04b_sample_alpha_all.bat`

#### 4b 开始前必须满足

1. 已运行步骤 2，且本地存在 `data/index/gmw_6deg_tiles.csv` 与 `data/index/gmw_1deg_cells.csv`。
2. 来源目录 `projects/my-project-2025924/assets/global_mangrove_subcanopy_terrain/gedi_points` 中的阶段 1 GEDI 表资产已完成；未导出的空瓦片允许不存在。
3. 上述目录及其直接 `TABLE` 子资产已经共享给 `helpful-weft-484410-i4` 凭证背后的 Google 账号。共享后至少应能读取 `gedi_points_000E_000N_2019_2025`。
4. 本机已有有效凭证：`%USERPROFILE%\.config\earthengine\projects\helpful-weft-484410-i4.json`，且该 project 可以创建 GEE Asset。
5. 不要同时启动两个 04b 窗口。单个来源目录和目标 project 会自动加本地锁。

#### 当前默认配置

```yaml
gee:
  project: helpful-weft-484410-i4  # AlphaEarth 输出资产的归属账号

sampling:
  gedi_source_asset_folder: projects/my-project-2025924/assets/global_mangrove_subcanopy_terrain/gedi_points
  staged_point_year_mode: all      # 同一瓦片保留 2019-2025 全部月度 GEDI 观测
  staged_alpha_year_mode: all      # 对 2019-2025 AlphaEarth 年度 embedding 取均值
  alpha_max_points_per_task: 10000
  alpha_initial_batch: 30
  alpha_refill_batch: 30
  alpha_poll_minutes: 10
  alpha_active_threshold: 10
```

双击后两个输入都直接回车即可使用以上默认值。第一个输入决定**输出**写入哪个 project；第二个输入决定从哪个已共享目录**读取** GEDI 点表，两者可以属于不同账号。

作用：

- 等阶段1资产完成后，从资产中按空间块采样AlphaEarth，输出为**当前执行账号自己的GEE Table Asset**。
- 默认每个空间块最多10,000个GEDI脚印；首批提交30块，每10分钟检查一次；当 `READY + RUNNING` 总数不超过10时，再补交30块。
- 每个任务只计算当前空间块覆盖的AlphaEarth影像。黑色窗口会持续运行；按 `Ctrl+C` 可以安全停止，重新双击会从任务清单恢复。
- 脚本会用中文询问“执行 project”和“GEDI来源目录”；都直接回车则使用 `config.yaml` 的当前 project 和默认目录。

本项目当前的已共享阶段 1 GEDI 来源目录为：

```text
projects/my-project-2025924/assets/global_mangrove_subcanopy_terrain/gedi_points
```

该目录已经批量共享。双击 `run_04b_sample_alpha_all.bat` 后，两个输入框均直接回车；程序会使用 `helpful-weft-484410-i4` 读取该来源并将结果保存到当前执行账号自己的资产目录：

```text
projects/helpful-weft-484410-i4/assets/global_mangrove_subcanopy_terrain/alpha_samples/source_<来源哈希>/
```

如果今后换来源目录或输出账号，只修改本机 `config.yaml`；公开仓库中供新用户复制的是 `config.example.yaml`，不会提交本机 `config.yaml`：

```yaml
gee:
  project: helpful-weft-484410-i4  # 步骤4b执行和表资产保存的账号

sampling:
  gedi_source_asset_folder: projects/my-project-2025924/assets/global_mangrove_subcanopy_terrain/gedi_points
```

如果共享权限不足，程序会显示无法读取的资产路径，不会提交错误任务，并写入 `logs/alpha_asset_source_issues_<project>_<来源哈希>.csv`。任务清单保存在 `logs/alpha_asset_jobs_<project>_<来源哈希>.csv`；失败清单保存在 `logs/alpha_asset_failures_<project>_<来源哈希>.csv`。

来源目录可以列出并不代表当前认证账号能实际加载其中的表资产。04b 会在每个瓦片开始前以当前凭证再次验证该表是可读取的 `TABLE`；缺失、无读取权限、空表，或本瓦片没有可采样 GEDI 点时，会写入上述来源问题清单并继续处理其余瓦片，不会中断整个批次。共享其他账号资产时，应在来源账号中将 `gedi_points` 文件夹及其全部子表共享给当前认证所用的 Google 账号邮箱；`project ID` 不是可授予读取权限的账号身份。

### 批量共享来源 GEDI 表资产

GEE Code Editor 的文件夹共享窗口没有递归共享已有子表的开关。若阶段 1 的 `gedi_points` 有很多 `TABLE` 资产，双击 `run_00c_grant_gedi_source_access.bat` 可一次完成授权。

这一步必须在本机临时以**来源资产拥有者**的 Google 账号认证。若还没有该账号的凭证，程序会打印一个授权链接；请把链接粘贴到已登录来源账号的浏览器中完成认证。凭证只保存在本机：`%USERPROFILE%\.config\earthengine\projects\<来源project>.json`，不会上传 GitHub。

程序依次要求输入：来源拥有者使用的 GEE project、来源 `gedi_points` 完整目录、授权方式，以及接收权限的 Google 邮箱。选择“所有人可读”会公开该目录和全部直接 `TABLE` 子资产；选择邮箱则仅授予该邮箱 Reader。程序会先输出 `logs/source_asset_access_<来源哈希>.csv` 授权清单，随后必须输入大写 `GRANT` 才会真正修改权限。

命令行方式如下。默认只预览，不会修改 ACL：

```powershell
.\.venv\Scripts\python.exe -m mangrove_terrain --config config.yaml grant-source-asset-access --owner-project my-project-2025924 --source-asset-folder projects/my-project-2025924/assets/global_mangrove_subcanopy_terrain/gedi_points --recipient target-account@example.com
```

确认清单无误后，增加 `--apply --yes` 执行批量授权：

```powershell
.\.venv\Scripts\python.exe -m mangrove_terrain --config config.yaml grant-source-asset-access --owner-project my-project-2025924 --source-asset-folder projects/my-project-2025924/assets/global_mangrove_subcanopy_terrain/gedi_points --recipient target-account@example.com --apply --yes
```

步骤4b如何跳过已完成块：

1. 每轮先列出当前账号的 AlphaEarth 输出资产文件夹。
2. 某块目标 Table Asset 存在时，立即标记为 `completed` 并跳过，不依赖旧日志。
3. 目标资产不存在而任务为 `READY/RUNNING` 时继续等待；任务显示完成但资产不存在时会重新提交。
4. 只对明确“空集合/无要素/无数据”的错误标记 `ignored_no_data` 且不重跑；例如 `Computed value is too large` 等其他错误写入失败清单，等待人工处理。

人工重跑失败块的 PowerShell 示例：

```powershell
.\.venv\Scripts\python.exe -m mangrove_terrain --config config.yaml schedule-alpha-assets --resubmit-failed
```

也可只重跑指定空间块：

```powershell
.\.venv\Scripts\python.exe -m mangrove_terrain --config config.yaml schedule-alpha-assets --resubmit-chunks 102W_012N_xm98p00000_y15p00000_d1p00000
```

### 12. 推荐：双击 `run_04c_download_alpha_assets.bat`

这是步骤4b全部完成后的推荐下载方式。它不会重新运行4b，也不会创建 Google Drive 或新的 GEE 导出任务。

程序会依次要求输入：

1. 下载使用的 GEE project，必须是已认证且有步骤4b输出资产读取权限的账号。
2. 步骤4b实际输出的 AlphaEarth 目录，例如：

```text
projects/your-project/assets/global_mangrove_subcanopy_terrain/alpha_samples/source_016c656d34b5
```

3. 是否检查资产数量：输入 `Y` 会先完整扫描并显示直属 `TABLE` 数量；输入 `N` 不做预先统计或4b本地清单对账，发现资产后立即开始下载。

下载文件写入 `outputs/raw_samples/`，默认同时下载3个。下载清单写入：

```text
logs/alpha_asset_downloads_<目录哈希>.csv
```

可以安全重复运行：清单状态为 `downloaded` 且 CSV 非空的资产会跳过；中断遗留的 `.part` 文件会在下次重新获取。

### 12.1 备用：双击 `run_04c_export_alpha_assets_to_drive.bat`

步骤4c会再次询问步骤4b使用的 project 与 GEDI来源目录；请填入与4b相同的值。它只读取步骤4b已经实际存在的 Table Asset，每次最多提交30个 Google Drive 导出任务。完成后，CSV会出现在：

```text
mangrove_gedi_alphaearth_samples/
```

102W_012N测试瓦片共产生8个空间块，合计20,146个GEDI脚印；其他瓦片会根据点密度自动递归切分。

按此前约869万条质量合格GEDI观测估算，步骤4b理论上至少需要约869个10,000点块，实际数量会因1°边界和空间密度增加。阶段1约201个表资产，在3个并行任务下预计数小时；步骤4b通常是数小时到1-2天，整体建议按1-3天安排，并以任务清单和 `staged_task_status_latest.csv` 的真实耗时为准。

速度判断：阶段1已实测完整 6°瓦片 `102W_012N` 约4.1分钟、约0.92 EECU秒，已经很轻量；阶段2仍是总耗时和EECU的主体，因为它必须为每一个GEDI脚印读取64维10m embedding。小块实测2,475个脚印约12秒、约440 EECU秒。多账号可把阶段2分散到不同 project，增加可并行任务和可用额度；结果定义不变，也不会丢失月度观测。

为什么步骤4b先导出到 GEE Asset：

- 资产存在性可直接验证，避免只凭本地日志误判完成。
- 可以跨账号读取阶段1 GEDI点表，并将步骤4b结果保存到当前账号。
- 失败或中断后可以准确恢复，不重复计算已完成空间块。

成功后会生成任务登记表：

```text
logs/gedi_asset_tasks_YYYYMMDD_HHMMSS.csv
logs/alpha_asset_jobs_<project>_<来源哈希>.csv
logs/alpha_asset_failures_<project>_<来源哈希>.csv
logs/alpha_asset_drive_tasks_YYYYMMDD_HHMMSS.csv
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

- 可以。阶段1和步骤4b都会读取自己的任务日志并查询GEE状态。
- 不要删除 `logs/gedi_asset_tasks_*.csv`、`logs/alpha_asset_jobs_*.csv` 和 `logs/alpha_asset_failures_*.csv`。

步骤4b默认首批与补批各提交 `30` 个任务，10分钟检查一次；如果想改，在 `config.yaml` 里改：

```yaml
sampling:
  alpha_initial_batch: 30
  alpha_refill_batch: 30
  alpha_poll_minutes: 10
  alpha_active_threshold: 10
```

### 13. 双击 `run_05_aggregate.bat`

打开后会先显示本步骤要读取的原始采样表和将生成的训练表；只有输入大写 `Y` 才开始全量中值聚合。直接回车、输入 `N` 或其它内容会安全取消，不读取或改写任何结果文件。

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

### 14. EGM2008 高程基准统一

#### 14.1 双击 `run_05b_convert_egm2008.bat`

**必须在 MEOW-14 步骤6b之前完成本步骤。** GEDI `elev_lowestmode` 原始值相对于 WGS84 椭球；全球不同区域会因大地水准面起伏出现数十米的系统性正负偏移。本步骤通过 PROJ 读取 EGM2008 格网，将标签统一为 EGM2008 正高。

运行前确认两个文件存在：

```text
data/mangrove_gedi_alphaearth_training.parquet
F:\VDatum\us_nga_egm08_25.tif
```

双击后会先显示用途。输入 `Y` 才会开始；直接回车则取消。程序不会修改原始 Parquet，不会删除任何样本，也不会启动 R、GEE 或模型训练。

默认输出：

```text
data/mangrove_gedi_alphaearth_training_egm2008.parquet
outputs/analysis/egm2008_elevation_diagnostics/
```

新 Parquet 的 `elev_median` 是 EGM2008 正高；`elev_median_wgs84` 保留原始 GEDI 椭球高；`egm2008_grid_shift_m` 记录 PROJ 施加的改正值。诊断目录含改正前后直方图、原始高程与格网改正关系图、1°网格空间图、候选异常点图、统计表和 `candidate_outliers_egm2008.csv`。候选阈值 `< -20 m` 或 `> 50 m` 只用于审查，**不会筛掉数据**。

默认约需 5 GB 可用磁盘空间和约 10-20 分钟。输出 Parquet 已存在时程序会保护原结果并停止；只有在 PowerShell 命令末尾显式增加 `--overwrite` 才会覆盖。

当前项目默认已将后续输入设为 EGM2008 Parquet。步骤5b完成后，先进行下一节的样本 QC 对照试验；不应再使用原始椭球高 Parquet。

### 15. 样本筛选试验：先比较，后决定

本节不改变生产训练数据，也不宣称任何筛选规则已提高真实地形精度。因为当前没有独立 LiDAR/RTK，全部结果仅用于比较 GEDI 聚合标签的重复观测稳定性、局地空间一致性和对 AlphaEarth 特征的内部可预测性。

`[-20, 50] m` 在这里是候选范围，而不是全球红树林的统一生态高程界限。红树林低平潮间带的生态背景可支持审查极端标签，但 EGM2008 正高不等同于局地潮位基准；不能用产品色标、极端样本比例或单篇局地研究确定全球硬阈值。完整依据、文献边界和规则定义见 [样本筛选试验说明](docs/SAMPLE_QC_EXPERIMENT.md)。

#### 15.1 双击 `run_05c_prepare_sample_qc.bat`

作用：从完整 EGM2008 Parquet 生成不含 64 个特征重复副本的轻量级 `qc_flags.parquet`，固定 seed=42 的 70/30 划分，并审计四类标签：`base_qa`、`range_candidate`、H3 局地 MAD 候选离群以及 `repeat_high_confidence`。

其中，只有 `elev_count >= 2` 且 `elev_iqr` 不超过 1、2 或 5 m 的像元才会进入多重访高置信代理集。单次观测即使 IQR 显示为 0 也不会被当成稳定标签。程序不会改写原始训练 Parquet，也不会删除样本。

成功后会生成：

```text
outputs/analysis/sample_qc_experiment/qc_flags.parquet
outputs/analysis/sample_qc_experiment/qc_retention_by_region.csv
outputs/analysis/sample_qc_experiment/qc_h3_summary.parquet
outputs/analysis/sample_qc_experiment/gedi_qc_pilot_plan.csv
outputs/analysis/sample_qc_experiment/qc_experiment_summary.json
outputs/analysis/sample_qc_experiment/figures/
```

全量试验将分批读取约 701 万像元，依赖磁盘性能，预计需要数十分钟和数 GB 临时空间。结果已存在时为防止误覆盖会停止；只有 PowerShell 命令显式增加 `--overwrite` 才会重建。

#### 15.2 GEDI 原始质量字段小样本：先检查、再提交

先双击 `run_05d_inspect_gedi_qc_bands.bat`。它只读取 GEDI 月度产品的 `bandNames()`，确认 `sensitivity`、`elev_sensitivity`、`surface_flag`、`num_detectedmodes`、`beam` 和 `solar_elevation` 等字段是否实际存在；不会提交 GEE 任务，也不会调用 AlphaEarth。

然后双击 `run_05e_preview_gedi_qc_pilot.bat`。它只显示 14 个固定 H3-8 空间块和将要导出的字段。确认后才双击 `run_05f_submit_gedi_qc_pilot.bat`，创建最多 14 个临时 GEE Table Assets。pilot 不重导全量 GEDI、不采样 AlphaEarth；字段存在也不自动成为全球筛选条件，必须结合数据字典、区域留存和后续独立验证判断。

#### 15.3 固定参数 ranger 内部对照和 Word 报告

先双击 `run_06g_prepare_sample_qc_model_inputs.bat`。它为 `base_qa`、`range_candidate` 和 `provisional_screened` 生成可比的 R 输入。三个候选集使用同一冻结划分、相同地区训练上限和同样的 64 个有效 AlphaEarth 特征，不重新调参。

再确保已完成 `run_06a_check_r.bat`，双击 `run_06h_evaluate_sample_qc_candidates.bat`。它使用固定 `ranger` 参数输出基线 `test30` 与未参与训练的多重访高置信代理集上的 RMSE、MAE、Bias、R2 和残差统计。这些不是 LiDAR/RTK 外部精度。

最后双击 `run_06i_create_sample_qc_report.bat`，输出：

```text
outputs/analysis/sample_qc_experiment/GEDI_红树林林下地形样本筛选试验报告.docx
```

报告会记录候选规则、逐区留存、空间覆盖、文献依据和已完成的内部对照。没有运行 R 对照时，报告会明确标注该章节尚无结果，不会编造指标。

### 16. 推荐生产流程：MEOW-14 分区建模

本流程使用 14 个 MEOW 派生区，而不是逐一训练 232 个原始生态区。预测变量严格为 AlphaEarth 时间均值 `A00-A63`；标签为 2019-2025 GEDI `elev_lowestmode` 的10 m像元中值。结果属于经验统计下推：本地 test30 只是对 GEDI 聚合标签的随机内部验证，不可替代 LiDAR/RTK 外部验证，也不能直接称作真实林下地形精度。

采用生态分区的依据是，大尺度森林高度研究已表明生态区随机森林可降低单一全域模型的区域非平稳性（Wu and Shi, 2023, *IEEE TGRS*）；GEDI 与连续遥感特征结合进行壁到壁制图也已有全球实践（Potapov et al., 2021, *Remote Sensing of Environment*）。这些研究支持分区基线的合理性，但不等同于证明本项目的真实地形精度。尤其是本项目暂不使用 DEM、SAR 或坐标特征，较 RFDTM 等多源林下地形方案更简化；获得独立 LiDAR/RTK 后仍须开展外部验证。

每一个双击入口都会先显示中文说明，只有输入大写 `Y` 才会执行。

#### 16.1 先检查 R：双击 `run_06a_check_r.bat`

检查 `Rscript.exe`、`ranger`、`data.table`、`ggplot2` 和 `MASS`。未检测到 R 时会显示 CRAN 下载地址和建议安装目录；直接回车确认、输入新目录修改，或输入 `N` 取消。成功后实际 R 路径会保存到 `config.yaml` 的 `regional_modeling.rscript_path`。

#### 16.2 准备区域样本：双击 `run_06b_prepare_meow14_training.bat`

运行前确认两项配置：

```yaml
regional_modeling:
  # 本机的 14 区 Shapefile，不会随 GitHub 公开仓库提交。
  region_shp: 区域划分结果/coastal_belt_irregular_mangrove_regions_shapefile/coastal_belt_irregular_mangrove_regions.shp
  # 步骤5b产生的完整 EGM2008 正高训练 Parquet。
  input_training_parquet: data/mangrove_gedi_alphaearth_training_egm2008.parquet
  # base_qa 生产训练结果与历史 qc_v001 结果隔离保存。
  output_dir: outputs/training/meow14_egm2008_baseqa_v002
  # [-20, 50] m 仅在步骤15中作敏感性对照，默认不在生产流程删样本。
  elevation_qc_enabled: false
  elevation_min_m: -20.0
  elevation_max_m: 50.0
```

程序用 `pyogrio + shapely + pyarrow` 分批读取 Parquet，严格要求每个像元只命中一个区域；任意未归属或多重归属都会终止，不会悄悄分配。随后按 `ae_x + ae_y + seed=42` 的稳定哈希划分约 70% `train` 和 30% `test`，同一 10 m 像元不会进入两个集合。默认不再按绝对高程范围删除 `base_qa` 样本；范围和多约束规则的比较必须通过步骤15完成，并在未来 LiDAR/RTK 外部验证后另行决定。

提供的 MEOW-14 面含自相交环。程序优先以 Shapely/GEOS 的 `buffer(0)` 修复，再检查面有效性及任意两区的面积重叠；这是为了避免 `make_valid()` 在该文件中将 SEE 的连续覆盖范围意外拆出孔洞。修复区域和方法会写入区域归属审计。若仍发现未归属或多重归属，程序会停止并保留坐标示例供核查，绝不按最近区域自动分配。

完成后会生成：

```text
outputs/training/meow14_egm2008_baseqa_v002/region_assignment_audit.json
outputs/training/meow14_egm2008_baseqa_v002/elevation_qc_audit.json
outputs/training/meow14_egm2008_baseqa_v002/elevation_qc_by_region.csv
outputs/training/meow14_egm2008_baseqa_v002/region_manifest.csv
outputs/training/meow14_egm2008_baseqa_v002/regions/<REG_CODE>/<REG_CODE>_all_with_split.parquet
outputs/training/meow14_egm2008_baseqa_v002/regions/<REG_CODE>/<REG_CODE>_train70.csv
outputs/training/meow14_egm2008_baseqa_v002/regions/<REG_CODE>/<REG_CODE>_test30.csv
```

`elevation_qc_audit.json` 与 `elevation_qc_by_region.csv` 仍会记录本轮是否启用生产标签范围筛选；默认应显示未筛选。`region_manifest.csv` 是后续 R 与 GEE 的唯一文件清单。它应有14行，分别为 `AFW, AFE, RSG, IND, EAS, SEW, SEE, OCN, AUS, PAC, AMW, GMX, CAR, AME`。

#### 16.3 逐区调参：双击 `run_06c_tune_meow14_ranger.bat`

每区只读取该区完整 `train70`，独立进行5次10%无放回子样本抽取。每个重复内的样本固定，并被全部24组参数共用：`trees=100/200/300`、`mtry=8/16`、`bagFraction=0.5/0.632`、`min.node.size=5/10`；`ranger` 明确使用 `replace=FALSE`。按重复 OOB RMSE 均值排名，取前10组参数的均值作为该区 GEE 参数，再在同一批训练子样本上进行 OOB 确认。

结果写入：

```text
outputs/training/meow14_egm2008_baseqa_v002/ranger_tuning/<REG_CODE>/ranger_oob_ranking.csv
outputs/training/meow14_egm2008_baseqa_v002/ranger_tuning/<REG_CODE>/ranger_top10_mean_params_for_gee.csv
outputs/training/meow14_egm2008_baseqa_v002/ranger_tuning/regional_tuning_summary.csv
```

#### 16.4 本地最终模型与精度：双击 `run_06d_evaluate_meow14_ranger.bat`

每区按自己的最优参数，用完整 `train70` 拟合一个本地 `ranger` 模型，再分批预测完整锁定 `test30`。输出各区以及全部区域的加权指标与宏平均指标：`RMSE`、`MAE`、`Bias`、`R2`、残差分位数，并按 `elev_count` 与 `elev_iqr` 生成标签稳定性诊断。

每区另输出一张观测-预测**二维核密度散点图**。图形以固定种子从该区 `test30` 抽取最多10,000点：每个点的颜色是该预览样本内的二维高斯核密度，而不是原始 GEDI 观测次数；颜色条显示核密度。图中黑色虚线为 1:1 线，红线为预览样本的普通最小二乘回归线及方程，坐标轴同范围且等比例。图内 `N`、`R2`、`RMSE`、`MAE`、`Bias` 始终由完整 `test30` 计算，**不**由最多10,000个作图点计算；`Bias = 预测值 - 观测值`。回归线和核密度仅用于解释图形，不替代完整测试集指标。

```text
outputs/training/meow14_egm2008_baseqa_v002/ranger_evaluation/regional_evaluation_summary.csv
outputs/training/meow14_egm2008_baseqa_v002/ranger_evaluation/regional_evaluation_overall_metrics.csv
outputs/training/meow14_egm2008_baseqa_v002/ranger_evaluation/regional_test_diagnostics_by_label_stability.csv
outputs/training/meow14_egm2008_baseqa_v002/ranger_evaluation/<REG_CODE>/observed_predicted_test30.png
outputs/training/meow14_egm2008_baseqa_v002/ranger_evaluation/figures/
```

旧的 `outputs/training/meow14/` 和 `outputs/training/meow14_egm2008_qc_v001/` 均为历史流程结果，不能与新的 `baseqa_v002` 结果混用，也不能作为本流程的独立外部验证精度。未来有 LiDAR/RTK 后，还需统一垂直基准和潮位参考，再按独立站点或空间块验证。

#### 16.5 上传14个 GEE 训练表

对每个区域，在 [Earth Engine Code Editor](https://code.earthengine.google.com/) 的 **Assets → NEW → Table upload** 上传对应的：

```text
outputs/training/meow14_egm2008_baseqa_v002/regions/<REG_CODE>/<REG_CODE>_train70.csv
```

坐标字段选择 `longitude` 与 `latitude`，坐标系为 `EPSG:4326`。上传到配置给出的目录，并使用精确名称 `<REG_CODE>_train70`。例如版本 `v001`、区域 EAS：

```text
projects/your-project/assets/global_mangrove_subcanopy_terrain/training/meow14_egm2008_baseqa_v002/EAS_train70
```

不要上传 `*_test30.csv` 到 GEE；它只用于本地内部评估。14个 CSV 都含 `sample_id`、`REG_CODE`、`split`、经纬度、标签稳定性字段和 `A00-A63`，模型实际只使用 `elev_median` 与 `A00-A63`。

#### 16.6 检查上传结果：双击 `run_06e_check_meow14_gee_assets.bat`

程序会逐一确认全部训练资产可读、类型为 `TABLE`、`split/elev_median/A00-A63` 完整，且有效 `train` 行数和本地 `region_manifest.csv` 一致。检查结果保存为：

```text
outputs/training/meow14_egm2008_baseqa_v002/gee_training_asset_check.csv
```

任一资产不可读时，先检查当前 `gee.project` 凭证、网页上传 project 与 Asset 路径是否一致。

#### 16.7 提交区域 GEE 模型：双击 `run_06f_submit_meow14_gee_models.bat`

按顺序操作：先选 `1` 提交 EAS smoke test；EAS 的 classifier asset 完成后，再选 `2` 提交 AME 最大区压力测试；确认最大区可完成后，选 `3` 持续调度全部14区。调度器会读取本地任务清单和实际 classifier asset，最多保留3个当前账号的活跃 GEE 任务。

每区的模型资产命名为：

```text
projects/your-project/assets/global_mangrove_subcanopy_terrain/models/meow14_egm2008_baseqa_v002/RF_MangroveSubcanopy_MEOW14_<REG_CODE>_Train70_egm2008_baseqa_v002
```

已存在的同版本 classifier asset 会直接标记为完成并跳过。失败任务只记录在 `logs/meow14_gee_model_jobs_<project>_egm2008_baseqa_v002.csv`，不会自动降采样、覆盖或重提；选项 `4` 才会显式重提失败区域。全14区调度也会生成一个包含参数、样本数与来源表路径的元数据 TABLE Asset。

本轮到此为止：不启动全球10 m预测，不做区域拼接或边界羽化。

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

### 新增或切换 GEE project 凭证

下面命令只打印授权链接，不会自动打开浏览器。请把链接复制到指定的浏览器窗口：

```powershell
.\.venv\Scripts\python.exe -m mangrove_terrain --config config.yaml auth-project --project ee-wyhao026
```

临时使用该 project 读取另一个账号已共享的 GEDI 资产并启动步骤4b自动调度，不修改 `config.yaml`：

```powershell
.\.venv\Scripts\python.exe -m mangrove_terrain --config config.yaml --project ee-wyhao026 schedule-alpha-assets --source-asset-folder projects/ee-wyhao00203/assets/global_mangrove_subcanopy_terrain/gedi_points
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

### 提交一块正式 GEDI 资产测试任务

```powershell
.\.venv\Scripts\python.exe -m mangrove_terrain --config config.yaml export-gedi-assets --tiles 102W_012N --years 2019-2025 --year-mode all
```

查看两阶段任务状态：

```powershell
.\.venv\Scripts\python.exe -m mangrove_terrain --config config.yaml check-staged-tasks
```

### 提交全量采样任务

```powershell
.\.venv\Scripts\python.exe -m mangrove_terrain --config config.yaml export-gedi-assets --years 2019-2025 --year-mode all
```

阶段1资产完成后，启动步骤4b AlphaEarth资产自动调度：

```powershell
.\.venv\Scripts\python.exe -m mangrove_terrain --config config.yaml schedule-alpha-assets
```

步骤4b全部完成后，提交步骤4c，将已验证资产导出到Google Drive：

```powershell
.\.venv\Scripts\python.exe -m mangrove_terrain --config config.yaml export-alpha-assets-to-drive
```

推荐直接下载到本地时，运行：

```powershell
.\.venv\Scripts\python.exe -m mangrove_terrain --config config.yaml download-alpha-assets --interactive
```

如果某些密集瓦片的阶段1任务失败，可以指定瓦片并按年拆分；阶段2仍然按空间块拆分：

```powershell
.\.venv\Scripts\python.exe -m mangrove_terrain --config config.yaml export-gedi-assets --tiles 018W_006N --years 2019-2025 --year-mode annual
```

### 本地聚合

```powershell
.\.venv\Scripts\python.exe -m mangrove_terrain --config config.yaml aggregate
```

### 统一到 EGM2008 高程基准

```powershell
.\.venv\Scripts\python.exe -m mangrove_terrain --config config.yaml convert-egm2008
```

如需明确覆盖已有的 EGM2008 输出文件：

```powershell
.\.venv\Scripts\python.exe -m mangrove_terrain --config config.yaml convert-egm2008 --overwrite
```

### 样本筛选试验

以下命令与双击入口对应。`prepare-sample-qc` 只创建独立 QC 标记，不改写原始 EGM2008 表；`export-gedi-qc-pilot` 默认是预览，只有明确给出 `--submit` 才会提交临时 GEE Table Assets。

```powershell
.\.venv\Scripts\python.exe -m mangrove_terrain --config config.yaml prepare-sample-qc
.\.venv\Scripts\python.exe -m mangrove_terrain --config config.yaml inspect-gedi-qc-bands
.\.venv\Scripts\python.exe -m mangrove_terrain --config config.yaml export-gedi-qc-pilot
# 已确认 pilot 计划和 GEDI 字段后才运行：
.\.venv\Scripts\python.exe -m mangrove_terrain --config config.yaml export-gedi-qc-pilot --submit
.\.venv\Scripts\python.exe -m mangrove_terrain --config config.yaml prepare-sample-qc-model-inputs
.\.venv\Scripts\python.exe -m mangrove_terrain --config config.yaml evaluate-sample-qc-candidates
.\.venv\Scripts\python.exe -m mangrove_terrain --config config.yaml create-sample-qc-report
```

### MEOW-14 分区建模

```powershell
.\.venv\Scripts\python.exe -m mangrove_terrain --config config.yaml check-r-environment --interactive
.\.venv\Scripts\python.exe -m mangrove_terrain --config config.yaml prepare-regional-training
.\.venv\Scripts\python.exe -m mangrove_terrain --config config.yaml tune-regional-ranger
.\.venv\Scripts\python.exe -m mangrove_terrain --config config.yaml evaluate-regional-ranger
# 先在网页上传14个 <REG_CODE>_train70.csv，并完成资产检查。
.\.venv\Scripts\python.exe -m mangrove_terrain --config config.yaml check-regional-gee-assets
# EAS smoke test：
.\.venv\Scripts\python.exe -m mangrove_terrain --config config.yaml submit-regional-gee-models --regions EAS --once
# AME 压力测试：
.\.venv\Scripts\python.exe -m mangrove_terrain --config config.yaml submit-regional-gee-models --regions AME --once
# 两项确认后，开始完整14区、最多3任务的持续调度：
.\.venv\Scripts\python.exe -m mangrove_terrain --config config.yaml submit-regional-gee-models --schedule
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

### 4. 步骤4b提示无法读取其他账号的 GEDI 资产

确认来源路径是以 `.../gedi_points` 结尾的完整目录，并由资产所有者把文件夹或全部子表资产共享给当前 Google 账号。步骤4b会显示具体资产路径；检查共享权限后重新启动调度器即可。

### 5. 步骤4b失败了怎么办

打开最新的 `logs/alpha_asset_failures_<project>_<来源哈希>.csv`。其中会记录空间块、来源资产、目标资产与 GEE 原始报错。

- `ignored_no_data` 表示明确没有数据，不需要重跑。
- `needs_manual_retry` 表示计算、权限或导出异常；修正原因后用 `--resubmit-failed` 或 `--resubmit-chunks` 显式重跑。

### 6. 为什么没有马上生成本地 CSV

全量采样数据较大。推荐先运行 `run_04c_download_alpha_assets.bat`，直接下载到：

```text
outputs/raw_samples/
```

如果直接下载受网络限制，再使用 `run_04c_export_alpha_assets_to_drive.bat`，从 Google Drive 下载 CSV 后放到同一目录，再运行聚合。

### 7. 可以中断后继续吗

可以。  
已经生成的索引、验证 shard、日志和下载表都不会自动删除。
重新运行时步骤4b会先检查实际 AlphaEarth 输出资产；资产存在的块绝不会重复提交，READY/RUNNING任务会继续等待，其他失败则进入失败清单。

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
