import os
import pathlib
import tempfile

# 所有 pytest 运行的测试统一把持久化目录指向临时目录，避免污染真实 storage/。
# 必须在任何测试模块 import main 之前设置（conftest 先于测试模块加载）。
# 独立运行 `python tests/test_pipeline.py` 时由该文件自身顶部兜底设置。
_TEST_STORAGE = pathlib.Path(tempfile.mkdtemp(prefix="opc_test_storage_"))
os.environ["OPC_STORAGE_DIR"] = str(_TEST_STORAGE)
