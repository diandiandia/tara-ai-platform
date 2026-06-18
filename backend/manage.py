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

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="TARA AI Platform CLI 管理工具")
    subparsers = parser.add_subparsers(dest="command", help="子命令")

    # init-db
    subparsers.add_parser("init-db", help="初始化数据库并创建表")

    # create-admin
    parser_admin = subparsers.add_parser("create-admin", help="创建初始管理员账号")
    parser_admin.add_argument("--user", required=True, help="管理员用户名")
    parser_admin.add_argument("--pwd", required=True, help="管理员密码")

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
    elif args.command == "backup":
        backup()
    elif args.command == "restore":
        restore(args.file)
    else:
        parser.print_help()
