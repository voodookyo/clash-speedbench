import sys
from pathlib import Path

# 让 tests 能 import 仓库根目录下的模块（clash_speedbench / speedbench_workers / ...）
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
