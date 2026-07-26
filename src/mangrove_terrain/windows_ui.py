from __future__ import annotations


GUIDES = {
    "auth": ("新增或更新 GEE 账号凭证", "程序会显示授权链接，请在指定浏览器完成 Earth Engine 认证。"),
    "grant_source_access": ("批量授予 GEDI 来源资产读取权限", "必须使用来源资产拥有者账号认证。程序先检查目录和全部表资产，再要求输入 GRANT 才会写入权限。"),
    "sync_config": ("同步配置文件", "将把当前版本新增的默认字段补入 config.yaml，不会覆盖你已有的设置。"),
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
    "r_check": ("步骤6a：检查 R/ranger 环境", "未找到 R 时会提示从 CRAN 下载，并允许确认或修改安装目录。"),
    "training_prepare": ("步骤6b：准备训练样本", "生成固定70/30划分、R 调参样本池和供 Earth Engine 网页上传的完整 CSV。"),
    "ranger_tuning": ("步骤6c：R/ranger 调参", "将运行 240 组随机森林参数和重复 OOB 评估，运行时间较长；可按 Ctrl+C 停止。"),
    "gee_models": ("步骤6d：提交 GEE 模型", "先确认训练 CSV 已在 Earth Engine 网页上传为 TABLE Asset；将提交70%和全部样本两个回归模型任务。"),
    "regional_prepare": ("MEOW-14 步骤6b：准备区域训练样本", "将读取聚合 Parquet，严格核验每个像元只归属一个区域，再为每区生成固定随机70/30划分。完成后会产生14个训练 CSV，供 R 和 GEE 使用。"),
    "regional_tune": ("MEOW-14 步骤6c：逐区 ranger 调参", "每区仅使用 train70，进行5次独立10%无放回抽样和24组参数的 OOB 比较。运行时间较长，可按 Ctrl+C 停止后重新运行。"),
    "regional_evaluate": ("MEOW-14 步骤6d：本地最终模型与测试精度", "每区以完整 train70 拟合，并只在固定 test30 上报告 RMSE、MAE、Bias 和 R2。这是 GEDI 标签的随机内部验证，不是 LiDAR/RTK 外部精度。"),
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
