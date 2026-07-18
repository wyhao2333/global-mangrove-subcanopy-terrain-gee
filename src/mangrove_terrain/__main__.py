from __future__ import annotations

import argparse
import sys

from . import (
    aggregate_samples,
    check_native_tasks,
    check_staged_tasks,
    ee_auth,
    export_native_tiles,
    export_samples,
    export_staged,
    inspect_gee,
    modeling_placeholder,
    prepare_gmw,
    validate_native,
)
from .config import ensure_output_dirs, load_config


def _years(value: str) -> list[int]:
    if "-" in value:
        start, end = value.split("-", 1)
        return list(range(int(start), int(end) + 1))
    return [int(v) for v in value.split(",") if v.strip()]


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="mangrove_terrain",
        description="全球红树林 GEDI + AlphaEarth 数据制备流程",
    )
    parser.add_argument("--config", default="config.yaml", help="配置文件路径，默认 config.yaml")
    parser.add_argument("--project", dest="project_override", default=None, help="本次运行使用的 GEE project，不修改 config.yaml")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("inspect-gee", help="检查 GEE 登录和数据集可访问性")

    p_auth = sub.add_parser("auth-project", help="手动复制授权链接，保存指定 project 的独立 GEE 凭证")
    p_auth.add_argument("--project", dest="auth_project", required=True, help="要保存凭证并验证权限的 GEE project ID")
    p_auth.add_argument("--auth-mode", default="localhost:0", help="本机回调地址，默认 localhost:0 自动选端口")

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

    sub.add_parser("check-native-tasks", help="检查原生瓦片 Drive 导出任务状态")
    sub.add_parser("check-staged-tasks", help="检查两阶段 GEDI 资产和 AlphaEarth 导出任务状态")

    p_agg = sub.add_parser("aggregate", help="本地按 AlphaEarth 10 m 像元聚合 GEDI 高程")
    p_agg.add_argument("--input-dir", default=None, help="原始 CSV/Parquet 所在目录")
    p_agg.add_argument("--output", default=None, help="输出 parquet 路径")

    sub.add_parser("model-placeholder", help="显示建模占位说明")

    args = parser.parse_args()
    if args.command == "auth-project":
        try:
            ee_auth.add_project_credentials(args.auth_project, auth_mode=args.auth_mode)
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
        elif args.command == "aggregate":
            aggregate_samples.run(cfg, input_dir=args.input_dir, output_path=args.output)
        elif args.command == "model-placeholder":
            modeling_placeholder.run()
    except Exception as exc:
        print(f"\n错误：{exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
