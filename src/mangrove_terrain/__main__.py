from __future__ import annotations

import argparse
import sys

from . import (
    aggregate_samples,
    alpha_asset_scheduler,
    asset_access,
    check_native_tasks,
    check_staged_tasks,
    download_alpha_assets,
    ee_auth,
    egm2008_conversion,
    export_alpha_assets_to_drive,
    export_native_tiles,
    export_samples,
    export_staged,
    inspect_gee,
    prepare_gmw,
    regional_gee_models,
    regional_ranger,
    regional_training,
    r_environment,
    validate_native,
    windows_ui,
)
from .config import ensure_output_dirs, load_config, sync_config


def _years(value: str) -> list[int]:
    if "-" in value:
        start, end = value.split("-", 1)
        return list(range(int(start), int(end) + 1))
    return [int(v) for v in value.split(",") if v.strip()]


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="mangrove_terrain",
        description="全球红树林 GEDI + AlphaEarth MEOW-14 分区建模流程",
    )
    parser.add_argument("--config", default="config.yaml", help="配置文件路径，默认 config.yaml")
    parser.add_argument("--project", dest="project_override", default=None, help="本次运行使用的 GEE project，不修改 config.yaml")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("inspect-gee", help="检查 GEE 登录和数据集可访问性")
    sub.add_parser("sync-config", help="同步 MEOW-14 配置，清理废弃样本 QC 并迁移旧版区域默认值")

    p_guide = sub.add_parser("windows-guide", help="供Windows双击脚本显示中文说明")
    p_guide.add_argument("name", choices=sorted(windows_ui.GUIDES))
    p_guide.add_argument("--confirm", action="store_true", help="显示中文确认提示")

    p_auth = sub.add_parser("auth-project", help="手动复制授权链接，保存指定 project 的独立 GEE 凭证")
    p_auth.add_argument("--project", dest="auth_project", default=None, help="要保存凭证并验证权限的 GEE project ID")
    p_auth.add_argument("--interactive", action="store_true", help="以中文提示输入 GEE project ID")
    p_auth.add_argument("--auth-mode", default="localhost:0", help="本机回调地址，默认 localhost:0 自动选端口")

    p_grant = sub.add_parser("grant-source-asset-access", help="批量授予来源 GEDI 表资产读取权限")
    p_grant.add_argument("--owner-project", default=None, help="来源资产拥有者认证所使用的 GEE project")
    p_grant.add_argument("--source-asset-folder", default=None, help="要授权的 gedi_points 目录完整路径")
    recipient_group = p_grant.add_mutually_exclusive_group()
    recipient_group.add_argument("--recipient", default=None, help="接收 Reader 权限的 Google 账号邮箱")
    recipient_group.add_argument("--anyone", action="store_true", help="设为所有人可读")
    p_grant.add_argument("--apply", action="store_true", help="真正写入资产 ACL；默认仅预览")
    p_grant.add_argument("--yes", action="store_true", help="非交互式 --apply 的二次确认")
    p_grant.add_argument("--interactive", action="store_true", help="以中文提示输入来源账号、目录和授权对象")

    p_prepare = sub.add_parser("prepare-gmw", help="切分 GMW 2020 红树林面")
    p_prepare.add_argument("--all", action="store_true", help="全量切分；不加时默认只生成 5 个 smoke-test shards")
    p_prepare.add_argument("--max-shards", type=int, default=None, help="最多生成多少个 shards")
    p_prepare.add_argument("--native-only", action="store_true", help="只生成原生6度瓦片索引和1个验证 shard（推荐）")

    p_sample = sub.add_parser("sample", help="采样 GEDI + AlphaEarth")
    p_sample.add_argument("--mode", choices=["local", "drive"], default=None, help="local 小样本下载；drive 正式提交导出任务")
    p_sample.add_argument("--max-shards", type=int, default=None, help="最多处理多少个 shards")
    p_sample.add_argument("--years", type=_years, default=None, help="年份，例如 2020 或 2019-2025")
    p_sample.add_argument("--year-mode", choices=["all", "annual"], default=None, help="all: 一个 shard 导出全部年份；annual: 按年拆分")
    p_sample.add_argument("--smoke", action="store_true", help="小样本测试：1 个 shard + 2020 + 本地下载")

    p_native = sub.add_parser("sample-native", help="直接原生瓦片联合采样（仅实验/回退）")
    p_native.add_argument("--mode", choices=["local", "drive"], default=None)
    p_native.add_argument("--max-tiles", type=int, default=None, help="最多处理多少个原生瓦片")
    p_native.add_argument("--tiles", nargs="*", default=None, help="指定瓦片，例如 102W_012N")
    p_native.add_argument("--years", type=_years, default=None, help="年份，例如 2020 或 2019-2025")
    p_native.add_argument("--year-mode", choices=["all", "annual"], default=None)
    p_native.add_argument("--smoke", action="store_true", help="本地限量下载一个原生瓦片")

    p_validate = sub.add_parser("validate-native", help="逐字段比较旧流程和原生瓦片新流程")
    p_validate.add_argument("--shard-id", default=None, help="用于对照的旧 shard；默认取索引第一项")
    p_validate.add_argument("--year", type=int, default=2020)
    p_validate.add_argument("--month", type=int, default=1)
    p_validate.add_argument("--limit", type=int, default=5000)

    for command, help_text in [
        ("export-gedi-assets", "阶段1：导出 GEDI 月度脚印为 GEE 表资产"),
        ("sample-alpha-assets", "阶段2：从 GEDI 表资产采样 AlphaEarth"),
    ]:
        staged = sub.add_parser(command, help=help_text)
        staged.add_argument("--max-tiles", type=int, default=None)
        staged.add_argument("--tiles", nargs="*", default=None, help="指定瓦片，例如 018W_006N")
        staged.add_argument("--years", type=_years, default=None)
        staged.add_argument("--year-mode", choices=["all", "annual"], default=None)
        if command == "sample-alpha-assets":
            staged.add_argument("--max-chunks", type=int, default=None, help="最多提交多少个空间块，用于小样本测试")
            staged.add_argument(
                "--source-asset-folder",
                default=None,
                help="阶段 1 GEDI 点表目录；可填写其他账号已共享的完整 GEE asset 路径",
            )

    p_scheduler = sub.add_parser("schedule-alpha-assets", help="步骤4b：自动调度 AlphaEarth 表资产导出")
    p_scheduler.add_argument("--source-asset-folder", default=None, help="阶段1 GEDI 点表来源目录")
    p_scheduler.add_argument("--tiles", nargs="*", default=None, help="仅处理指定原生瓦片")
    p_scheduler.add_argument("--max-tiles", type=int, default=None, help="最多规划多少个原生瓦片")
    p_scheduler.add_argument("--max-jobs", type=int, default=None, help="最多规划多少个空间块，用于测试")
    p_scheduler.add_argument("--interactive", action="store_true", help="以中文提示输入执行project和来源资产目录")
    p_scheduler.add_argument("--once", action="store_true", help="只执行一轮检查和提交，不进入10分钟循环")
    p_scheduler.add_argument("--resubmit-chunks", nargs="*", default=None, help="人工指定重新提交的空间块ID")
    p_scheduler.add_argument("--resubmit-failed", action="store_true", help="人工确认后重新提交失败清单中的全部任务")
    p_scheduler.add_argument("--initial-batch", type=int, default=None)
    p_scheduler.add_argument("--refill-batch", type=int, default=None)
    p_scheduler.add_argument("--poll-minutes", type=float, default=None)
    p_scheduler.add_argument("--active-threshold", type=int, default=None)

    p_drive = sub.add_parser("export-alpha-assets-to-drive", help="步骤4c：将完成的 AlphaEarth 表资产导出到Drive")
    p_drive.add_argument("--source-asset-folder", default=None, help="阶段1 GEDI 点表来源目录")
    p_drive.add_argument("--interactive", action="store_true", help="以中文提示输入步骤4b使用的project和来源资产目录")
    p_drive.add_argument("--max-new-tasks", type=int, default=None)
    p_drive.add_argument("--force-assets", nargs="*", default=None, help="仅导出指定的阶段2表资产")

    p_local_download = sub.add_parser("download-alpha-assets", help="步骤4c：直接下载 AlphaEarth 表资产到本地")
    p_local_download.add_argument("--asset-folder", default=None, help="步骤4b AlphaEarth 输出目录完整路径")
    p_local_download.add_argument("--interactive", action="store_true", help="以中文提示输入 project、输出目录和数量检查选项")
    count_group = p_local_download.add_mutually_exclusive_group()
    count_group.add_argument("--check-asset-count", dest="check_asset_count", action="store_true")
    count_group.add_argument("--skip-asset-count-check", dest="check_asset_count", action="store_false")
    p_local_download.set_defaults(check_asset_count=None)
    p_local_download.add_argument("--workers", type=int, default=None, help="本地下载并发数")

    sub.add_parser("check-native-tasks", help="检查原生瓦片 Drive 导出任务状态")
    sub.add_parser("check-staged-tasks", help="检查两阶段 GEDI 资产和 AlphaEarth 导出任务状态")

    p_agg = sub.add_parser("aggregate", help="本地按 AlphaEarth 10 m 像元聚合 GEDI 高程")
    p_agg.add_argument("--input-dir", default=None, help="原始 CSV/Parquet 所在目录")
    p_agg.add_argument("--output", default=None, help="输出 parquet 路径")

    p_egm2008 = sub.add_parser("convert-egm2008", help="将 GEDI 聚合高程从 WGS84 椭球高改正为 EGM2008 正高")
    p_egm2008.add_argument("--input", default=None, help="输入聚合训练 Parquet 路径")
    p_egm2008.add_argument("--output", default=None, help="输出 EGM2008 训练 Parquet 路径")
    p_egm2008.add_argument("--grid", default=None, help="PROJ 可读取的 EGM2008 GeoTIFF 路径")
    p_egm2008.add_argument("--analysis-dir", default=None, help="统计表、图件和候选异常点输出目录")
    p_egm2008.add_argument("--overwrite", action="store_true", help="明确允许覆盖已有 EGM2008 输出 Parquet")

    p_r_check = sub.add_parser("check-r-environment", help="步骤6a：检查或安装 R/ranger 环境")
    p_r_check.add_argument("--interactive", action="store_true", help="找不到 R 时按中文提示下载安装")
    p_regional_prepare = sub.add_parser("prepare-regional-training", help="MEOW-14：区域归属、固定随机划分与训练文件准备")
    p_regional_prepare.add_argument("--input", default=None, help="聚合训练 Parquet 路径")
    p_regional_prepare.add_argument("--output-dir", default=None, help="MEOW-14 输出目录")
    p_regional_prepare.add_argument("--batch-rows", type=int, default=100000, help="每批读取的 Parquet 行数")
    p_regional_prepare.add_argument("--max-rows", type=int, default=None, help="仅处理前 N 行，供小样本验证")
    p_regional_prepare.add_argument("--overwrite", action="store_true", help="明确删除旧的 MEOW-14 输出目录后重建")
    p_regional_tune = sub.add_parser("tune-regional-ranger", help="MEOW-14：逐区 ranger 重复 OOB 调参")
    p_regional_tune.add_argument("--quick", action="store_true", help="仅 1 次重复和 2 组参数，用于环境测试")
    p_regional_tune.add_argument("--regions", nargs="*", default=None, help="仅运行指定区域，如 EAS AME")
    p_regional_evaluate = sub.add_parser("evaluate-regional-ranger", help="MEOW-14：完整 train70 本地拟合与 test30 评估")
    p_regional_evaluate.add_argument("--quick", action="store_true", help="仅评估第一个区域，用于环境测试")
    p_regional_evaluate.add_argument("--regions", nargs="*", default=None, help="仅运行指定区域，如 EAS AME")
    p_regional_check = sub.add_parser("check-regional-gee-assets", help="MEOW-14：检查 14 个网页上传的 Train70 TABLE Assets")
    p_regional_check.add_argument("--regions", nargs="*", default=None, help="仅检查指定区域")
    p_regional_check.add_argument("--skip-count-check", action="store_true", help="只检查资产类型和读取权限，不逐表统计行数")
    p_regional_models = sub.add_parser("submit-regional-gee-models", help="MEOW-14：提交区域 GEE 回归随机森林")
    p_regional_models.add_argument("--regions", nargs="*", default=None, help="仅提交指定区域，如 EAS AME")
    p_regional_models.add_argument("--once", action="store_true", help="只检查并提交一轮可用空位，然后退出")
    p_regional_models.add_argument("--schedule", action="store_true", help="持续调度，始终最多 3 个总活跃任务")
    p_regional_models.add_argument("--resubmit-failed", action="store_true", help="显式允许重新提交本地失败清单中的区域")
    p_regional_models.add_argument("--interactive", action="store_true", help="以中文选择 EAS smoke、AME 压力测试或全部调度")

    args = parser.parse_args()
    if args.command == "windows-guide":
        if not windows_ui.run(args.name, confirm=args.confirm):
            raise SystemExit(2)
        return
    if args.command == "sync-config":
        path = sync_config(args.config)
        print(f"配置已同步到当前 MEOW-14 版本: {path}")
        return
    if args.command == "auth-project":
        try:
            auth_project = args.auth_project
            if args.interactive:
                auth_project = input("请输入要认证的 GEE project ID，例如 ee-wyhao026: ").strip()
            if not auth_project:
                raise ValueError("必须提供 GEE project ID。")
            ee_auth.add_project_credentials(auth_project, auth_mode=args.auth_mode)
        except Exception as exc:
            print(f"\n错误：{exc}", file=sys.stderr)
            raise SystemExit(1) from exc
        return

    cfg = load_config(args.config)
    if args.project_override:
        cfg["gee"]["project"] = args.project_override
    ensure_output_dirs(cfg)

    try:
        if args.command == "inspect-gee":
            inspect_gee.run(cfg)
        elif args.command == "grant-source-asset-access":
            asset_access.run(
                cfg,
                owner_project=args.owner_project,
                source_asset_folder=args.source_asset_folder,
                recipient=args.recipient,
                anyone=args.anyone,
                apply=args.apply,
                yes=args.yes,
                interactive=args.interactive,
            )
        elif args.command == "prepare-gmw":
            prepare_gmw.run(
                cfg,
                all_shards=args.all,
                max_shards=args.max_shards,
                native_only=args.native_only,
            )
        elif args.command == "sample":
            export_samples.run(
                cfg,
                mode=args.mode,
                max_shards=args.max_shards,
                years=args.years,
                year_mode=args.year_mode,
                smoke=args.smoke,
            )
        elif args.command == "sample-native":
            export_native_tiles.run(
                cfg,
                mode=args.mode,
                max_tiles=args.max_tiles,
                tile_ids=args.tiles,
                years=args.years,
                year_mode=args.year_mode,
                smoke=args.smoke,
            )
        elif args.command == "validate-native":
            validate_native.run(
                cfg,
                shard_id=args.shard_id,
                year=args.year,
                month=args.month,
                limit=args.limit,
            )
        elif args.command == "check-native-tasks":
            check_native_tasks.run(cfg)
        elif args.command == "check-staged-tasks":
            check_staged_tasks.run(cfg)
        elif args.command == "export-gedi-assets":
            export_staged.export_gedi_assets(
                cfg,
                max_tiles=args.max_tiles,
                tile_ids=args.tiles,
                years=args.years,
                year_mode=args.year_mode,
            )
        elif args.command == "sample-alpha-assets":
            export_staged.export_alpha_samples(
                cfg,
                max_tiles=args.max_tiles,
                tile_ids=args.tiles,
                years=args.years,
                year_mode=args.year_mode,
                max_chunks=args.max_chunks,
                source_asset_folder=args.source_asset_folder,
            )
        elif args.command == "schedule-alpha-assets":
            alpha_asset_scheduler.run(
                cfg,
                source_asset_folder=args.source_asset_folder,
                tile_ids=args.tiles,
                max_tiles=args.max_tiles,
                max_jobs=args.max_jobs,
                interactive=args.interactive,
                once=args.once,
                resubmit_chunks=args.resubmit_chunks,
                resubmit_failed=args.resubmit_failed,
                initial_batch=args.initial_batch,
                refill_batch=args.refill_batch,
                poll_minutes=args.poll_minutes,
                active_threshold=args.active_threshold,
            )
        elif args.command == "export-alpha-assets-to-drive":
            export_alpha_assets_to_drive.run(
                cfg,
                source_asset_folder=args.source_asset_folder,
                max_new_tasks=args.max_new_tasks,
                force_assets=args.force_assets,
                interactive=args.interactive,
            )
        elif args.command == "download-alpha-assets":
            download_alpha_assets.run(
                cfg,
                asset_folder=args.asset_folder,
                interactive=args.interactive,
                check_asset_count=args.check_asset_count,
                workers=args.workers,
            )
        elif args.command == "aggregate":
            aggregate_samples.run(cfg, input_dir=args.input_dir, output_path=args.output)
        elif args.command == "convert-egm2008":
            egm2008_conversion.run(
                cfg,
                input_path=args.input,
                output_path=args.output,
                grid_path=args.grid,
                analysis_dir=args.analysis_dir,
                overwrite=args.overwrite,
            )
        elif args.command == "check-r-environment":
            r_environment.run(cfg, config_path=args.config, interactive=args.interactive)
        elif args.command == "prepare-regional-training":
            regional_training.run(
                cfg,
                source=args.input,
                destination=args.output_dir,
                batch_rows=args.batch_rows,
                max_rows=args.max_rows,
                overwrite=args.overwrite,
            )
        elif args.command == "tune-regional-ranger":
            regional_ranger.tune(cfg, quick=args.quick, region_codes=args.regions)
        elif args.command == "evaluate-regional-ranger":
            regional_ranger.evaluate(cfg, quick=args.quick, region_codes=args.regions)
        elif args.command == "check-regional-gee-assets":
            regional_gee_models.check_assets(
                cfg,
                region_codes=args.regions,
                include_count=not args.skip_count_check,
            )
        elif args.command == "submit-regional-gee-models":
            if args.interactive:
                regional_gee_models.interactive_submit(cfg)
            else:
                regional_gee_models.submit(
                    cfg,
                    region_codes=args.regions,
                    once=args.once,
                    schedule=args.schedule,
                    resubmit_failed=args.resubmit_failed,
                )
    except Exception as exc:
        print(f"\n错误：{exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
