from __future__ import annotations


GUIDES = {
    "auth": ("新增或更新 GEE 账号凭证", "程序会显示授权链接，请在指定浏览器完成 Earth Engine 认证。"),
    "grant_source_access": ("批量授予 GEDI 来源资产读取权限", "必须使用来源资产拥有者账号认证。程序先检查目录和全部表资产，再要求输入 GRANT 才会写入权限。"),
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
    "aggregate": ("步骤5：本地中值聚合", "将读取 outputs/raw_samples 中下载的CSV或Parquet，并生成训练表。"),
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
