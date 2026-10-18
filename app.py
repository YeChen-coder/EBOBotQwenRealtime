"""应用入口点"""
from pathlib import Path

from dotenv import load_dotenv

# Docker 里配置由 env_file 注入（优先级更高）；直接在本机运行时从 .env 读取
load_dotenv(Path(__file__).resolve().parent / '.env')

from runtime import main  # noqa: E402  配置需在导入前加载

if __name__ == "__main__":
    main()
