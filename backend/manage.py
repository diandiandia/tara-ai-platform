#!/usr/bin/env python3
import os
import sys
import argparse
import tarfile
import shutil
from datetime import datetime

# 保证当前路径在 Python path 内
sys.path.append(os.path.abspath(os.path.dirname(__file__)))

from app.core.database import Base, engine, SessionLocal
from app.models.user import User
from app.models.project import Project
from app.models.domain import Domain
from app.models.diagram import Diagram
from app.models.asset import Asset
from app.models.tara_run import TaraRun
from app.models.tara_step import TaraStep
from app.models.system_settings import SystemSettings

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_FILE = os.path.join(PROJECT_ROOT, "tara_local.db")
EXPORTS_DIR = os.path.join(PROJECT_ROOT, "exports_local")
BACKUP_DIR = os.path.join(PROJECT_ROOT, "backups_local")

def init_db():
    print("正在初始化数据库表...")
    Base.metadata.create_all(bind=engine)
    print("数据库表初始化成功！")

def create_admin(username, password):
    from bcrypt import hashpw, gensalt
    db = SessionLocal()
    try:
        # 检查是否已存在
        existing = db.query(User).filter(User.username == username).first()
        if existing:
            print(f"用户 '{username}' 已存在。")
            return
        
        # 密码加密
        pwd_bytes = password.encode('utf-8')
        hashed = hashpw(pwd_bytes, gensalt()).decode('utf-8')
        
        admin = User(username=username, password_hash=hashed, role="admin")
        db.add(admin)
        db.commit()
        print(f"管理员用户 '{username}' 创建成功！")
    except Exception as e:
        db.rollback()
        print(f"创建管理员用户失败: {e}")
    finally:
        db.close()

def backup():
    print("正在启动系统数据备份...")
    if not os.path.exists(BACKUP_DIR):
        os.makedirs(BACKUP_DIR)
        
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_file = os.path.join(BACKUP_DIR, f"tara_backup_{timestamp}.tar.gz")
    
    try:
        with tarfile.open(backup_file, "w:gz") as tar:
            # 备份数据库文件
            if os.path.exists(DB_FILE):
                tar.add(DB_FILE, arcname="tara.db")
                print(f"-> 数据库已加入备份: {DB_FILE}")
            else:
                print("⚠️ 数据库文件不存在，跳过数据库物理备份。")
                
            # 备份导出报告文件夹
            if os.path.exists(EXPORTS_DIR):
                tar.add(EXPORTS_DIR, arcname="exports")
                print(f"-> 报告文件夹已加入备份: {EXPORTS_DIR}")
            else:
                print("-> 报告文件夹不存在，跳过报告备份。")
                
        print(f"🎉 备份成功！备份文件保存在: {backup_file}")
    except Exception as e:
        print(f"❌ 备份失败: {e}")

def restore(backup_path):
    print(f"正在启动系统数据恢复，备份包: {backup_path}...")
    if not os.path.exists(backup_path):
        print(f"❌ 错误: 备份包文件 {backup_path} 不存在。")
        return
        
    try:
        # 临时解压目录
        temp_dir = "/tmp/tara_restore_temp"
        if os.path.exists(temp_dir):
            shutil.rmtree(temp_dir)
        os.makedirs(temp_dir)
        
        with tarfile.open(backup_path, "r:gz") as tar:
            tar.extractall(path=temp_dir)
            
        # 恢复数据库
        db_source = os.path.join(temp_dir, "tara.db")
        if os.path.exists(db_source):
            # 停止当前数据库连接（这里只是脚本，覆盖文件即可）
            shutil.copy2(db_source, DB_FILE)
            print(f"-> 数据库已成功覆盖恢复: {DB_FILE}")
        else:
            print("⚠️ 备份包内无数据库文件，未恢复数据库。")
            
        # 恢复报告文件夹
        exports_source = os.path.join(temp_dir, "exports")
        if os.path.exists(exports_source):
            if os.path.exists(EXPORTS_DIR):
                shutil.rmtree(EXPORTS_DIR)
            shutil.copytree(exports_source, EXPORTS_DIR)
            print(f"-> 报告目录已成功覆盖恢复: {EXPORTS_DIR}")
        else:
            print("-> 备份包内无报告目录，未恢复报告。")
            
        # 清理临时目录
        shutil.rmtree(temp_dir)
        print("🎉 恢复完成！系统数据已原子重建。")
    except Exception as e:
        print(f"❌ 恢复失败: {e}")

def migrate():
    """为已有数据库补齐模型新增的列和 TARA 步骤索引（轻量幂等迁移）。

    新装环境用 init-db 建全表即可；已有库在升级代码后执行本命令，
    避免因为缺少新列（如 tara_csr_mapping.conflict_note）而报错。
    """
    from sqlalchemy import inspect, text

    Base.metadata.create_all(bind=engine)
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    added = 0
    for table in Base.metadata.sorted_tables:
        if table.name not in existing_tables:
            continue
        db_columns = {col["name"] for col in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name in db_columns or column.primary_key:
                continue
            if not column.nullable and column.server_default is None:
                print(f"⚠️ 跳过 {table.name}.{column.name}（NOT NULL 且无默认值，需手工迁移）")
                continue
            ddl_type = column.type.compile(dialect=engine.dialect)
            try:
                with engine.begin() as conn:
                    conn.execute(text(
                        f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {ddl_type}'
                    ))
            except Exception as exc:  # noqa: BLE001 - report and continue
                print(f"⚠️ {table.name}.{column.name} 迁移失败: {exc}")
                continue
            print(f"-> {table.name}.{column.name} 已补齐 ({ddl_type})")
            added += 1

    # create_all 不会补齐已存在表上的新索引。
    for index in TaraStep.__table__.indexes:
        index.create(bind=engine, checkfirst=True)

    if added:
        print(f"🎉 迁移完成，共补齐 {added} 个字段。")
    else:
        print("数据库字段已是最新，TARA 步骤索引已检查补齐。")


def cleanup_history():
    """Use an online SQLite backup and reserve the writer before inspecting runs."""
    import sqlite3
    from sqlalchemy import text
    from app.services.tara_history import cleanup_tara_history

    if engine.dialect.name != "sqlite":
        raise RuntimeError("本命令仅支持 SQLite；其他数据库请先建立一致性备份。")
    os.makedirs(BACKUP_DIR, exist_ok=True)
    backup_path = os.path.join(
        BACKUP_DIR, f"before_history_cleanup_{datetime.now():%Y%m%d_%H%M%S_%f}.db"
    )
    fd = os.open(backup_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    raw = engine.raw_connection()
    try:
        with sqlite3.connect(backup_path) as target:
            raw.driver_connection.backup(target)
            if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("备份完整性检查失败，未清理历史。")
    finally:
        raw.close()
    print(f"一致性备份: {backup_path}")
    with SessionLocal() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        result = cleanup_tara_history(db)
        db.commit()
    print(result)
    print("清理完成；空闲页可供后续写入复用，未执行 VACUUM。")


FIXTURE_CATALOG = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "fixtures", "csr_new.xlsx"
)


def import_csr_catalog_command(file_path, sheet_name=None, activate=True, if_missing=False):
    """导入固定 CSR 目录 xlsx；相同内容会复用已有版本。"""
    from app.services.csr_catalog import get_active_catalog_version, import_csr_catalog

    if not file_path or not os.path.exists(file_path):
        print(f"❌ 固定 CSR 目录文件不存在: {file_path}")
        raise SystemExit(1)

    db = SessionLocal()
    try:
        if if_missing:
            active = get_active_catalog_version(db)
            if active is not None:
                print(
                    f"固定 CSR 目录已存在（{active.version}，"
                    f"{active.row_count} 条），跳过导入。"
                )
                return
        try:
            result = import_csr_catalog(
                db, file_path, sheet_name=sheet_name, activate=activate
            )
        except Exception as exc:  # noqa: BLE001 - surface the reason and exit non-zero
            print(f"❌ 固定 CSR 目录导入失败: {exc}")
            raise SystemExit(1)
        print(
            f"✅ 固定 CSR 目录已导入: {result['version']} "
            f"({result['row_count']} 条, sheet={result['sheet_name']}, active={result['active']})"
        )
    finally:
        db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="TARA AI Platform CLI 管理工具")
    subparsers = parser.add_subparsers(dest="command", help="子命令")

    # init-db
    subparsers.add_parser("init-db", help="初始化数据库并创建表")

    # create-admin
    parser_admin = subparsers.add_parser("create-admin", help="创建初始管理员账号")
    parser_admin.add_argument("--user", required=True, help="管理员用户名")
    parser_admin.add_argument("--pwd", required=True, help="管理员密码")

    # migrate
    subparsers.add_parser("migrate", help="为已有数据库补齐模型新增字段（幂等）")
    subparsers.add_parser("cleanup-history", help="备份并清理历史，保留每域控最新成功及最新任务")

    # import-csr-catalog
    parser_catalog = subparsers.add_parser(
        "import-csr-catalog", help="导入固定 CSR 目录 xlsx（同内容自动复用版本）"
    )
    parser_catalog.add_argument(
        "--file", default=FIXTURE_CATALOG,
        help="目录 xlsx 路径（默认 backend/fixtures/csr_new.xlsx）",
    )
    parser_catalog.add_argument("--sheet", default=None, help="指定 sheet（默认自动识别）")
    parser_catalog.add_argument("--no-activate", action="store_true", help="只导入不激活")
    parser_catalog.add_argument(
        "--if-missing", action="store_true", help="已有生效目录时跳过"
    )

    # backup
    subparsers.add_parser("backup", help="一键备份数据库与报告文件")

    # restore
    parser_restore = subparsers.add_parser("restore", help="一键从备份包中恢复数据")
    parser_restore.add_argument("--file", required=True, help="备份包 .tar.gz 文件路径")

    args = parser.parse_args()

    if args.command == "init-db":
        init_db()
    elif args.command == "create-admin":
        create_admin(args.user, args.pwd)
    elif args.command == "migrate":
        migrate()
    elif args.command == "cleanup-history":
        cleanup_history()
    elif args.command == "import-csr-catalog":
        import_csr_catalog_command(
            args.file, args.sheet, not args.no_activate, args.if_missing
        )
    elif args.command == "backup":
        backup()
    elif args.command == "restore":
        restore(args.file)
    else:
        parser.print_help()
