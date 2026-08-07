from __future__ import annotations


GUIDES = {
    "auth": ("新增或更新 GEE 账号凭证", "程序会显示授权链接，请在指定浏览器完成 Earth Engine 认证。"),
    "grant_source_access": ("批量授予 GEDI 来源资产读取权限", "必须使用来源资产拥有者账号认证。程序先检查目录和全部表资产，再要求输入 GRANT 才会写入权限。"),
    "sync_config": ("同步 MEOW-14 配置", "将补入当前区域流程所需字段，移除已废弃的 sample_qc_experiment，并把旧版 baseqa/qc 版本迁移到当前 [-20, 50] m 默认流程；新版本中的自定义区域设置会保留。"),
    "check": ("检查 GEE 环境", "将检查当前账号、GEDI、AlphaEarth 和 GMW 公共数据集是否可用。"),
    "prepare": ("生成 GMW 索引", "将读取本地 GMW 矢量，生成全球原生GEDI瓦片与空间块索引。"),
    "validate": ("验证采样一致性", "将比较旧流程和新流程的小样本字段，任何差异都会报错。"),
    "native_test": ("原生瓦片对照测试", "仅提交一个旧式直接联合采样任务，用于与正式两阶段流程对照。"),
    "native_status": ("检查原生瓦片任务", "将显示旧式直接联合采样任务的完成、运行和失败状态。"),
    "stage1_test": ("步骤4小样本测试", "仅提交一个完整6度GEDI点表资产，用于验证阶段1导出。"),
    "stage1": ("步骤4：导出 GEDI 点表资产", "将把2019-2025全部质量合格的GEDI月度脚印保存为GEE表资产。"),
    "stage2": ("步骤4b：自动生成 AlphaEarth 表资产", "首次提交30块，之后每10分钟检查一次；可按 Ctrl+C 安全停止。"),
    "stage2_test": ("步骤4b小样本测试", "仅提交一个 AlphaEarth 空间块，用于检查资产导出是否正常。"),
    "stage2_status": ("检查两阶段任务状态", "将显示已提交任务的完成、运行和失败状态。"),
    "stage2_drive": ("步骤4c：导出表资产到 Google Drive", "仅导出已验证存在的 AlphaEarth 表资产，供后续下载和本地聚合。"),
    "stage2_local_download": ("步骤4c：直接下载 AlphaEarth 表资产", "读取步骤4b已经完成的 AlphaEarth 输出目录，直接下载 CSV 到 outputs/raw_samples，不会创建 Drive 或 GEE 导出任务。"),
    "aggregate": ("步骤5：本地中值聚合", "将读取 outputs/raw_samples 中下载的CSV或Parquet，并生成训练表。"),
    "egm2008": ("步骤5b：统一 GEDI 高程基准到 EGM2008", "将读取完整聚合训练 Parquet，保留原始 WGS84 椭球高并生成新的 EGM2008 训练表、分布统计和异常候选点审查图。不会删除样本、不会修改原始文件，也不会启动 R 或 GEE 任务。默认需要约 5 GB 可用磁盘空间。"),
    "nz_lidar_validation": ("步骤05c：NZ LiDAR 外部一致性验证", "将用 NZ 1 m LiDAR DEM 的 25 m 圆形足迹中值核对 EGM2008 GEDI 聚合标签。GEDI 标签是 2019-2025 全期中值，LiDAR 是 2018-2023 调查数据；结果只能称为存在名义年份重叠的外部一致性验证，不是严格同期验证。程序不会修改 Parquet、不会创建 GEE 任务。"),
    "r_check": ("步骤6a：检查 R/ranger 环境", "未找到 R 时会提示从 CRAN 下载，并允许确认或修改安装目录。"),
    "regional_prepare": ("MEOW-14 步骤6b：准备区域训练样本", "将读取 EGM2008 聚合 Parquet，严格核验每个像元只归属一个区域；随后按配置的闭区间 [-20, 50] m 做标签完整性筛选，再为每区生成固定随机70/30划分和全局、逐区质控审计。"),
    "regional_tune": ("MEOW-14 步骤6c：逐区 ranger 调参", "每区仅使用 train70，进行5次独立10%无放回抽样和24组参数的 OOB 比较。运行时间较长，可按 Ctrl+C 停止后重新运行。"),
    "regional_evaluate": ("MEOW-14 步骤6d：本地最终模型与测试精度", "每区以完整 train70 拟合，并只在固定 test30 上报告 RMSE、MAE、Bias 和 R2；每区另生成含完整测试集指标和1:1线的散点图。这是 GEDI 标签的随机内部验证，不是 LiDAR/RTK 外部精度。"),
    "regional_gee_check": ("MEOW-14 步骤6e：检查 GEE 区域训练表", "请先在 Earth Engine 网页上传14个 *_train70.csv。程序会逐一核对 TABLE 类型、必需字段和有效训练样本数。"),
    "regional_gee_models": ("MEOW-14 步骤6f：提交 GEE 区域模型", "先选择 EAS 小区 smoke test，再选择 AME 最大区压力测试；二者完成后再启动全部14区调度。调度器始终最多保留3个总活跃任务。"),
}


def run(name: str, confirm: bool = False) -> bool:
    title, detail = GUIDES[name]
    print(f"\n{'=' * 68}\n{title}\n{'=' * 68}")
    print(detail)
    if not confirm:
        return True
    answer = input("确认继续执行？输入 Y 继续，直接回车或输入其他内容取消: ").strip().upper()
    if answer == "Y":
        return True
    print("已取消，未提交任何任务。")
    return False
