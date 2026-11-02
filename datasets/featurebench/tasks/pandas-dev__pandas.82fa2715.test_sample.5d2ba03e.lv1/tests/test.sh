#!/usr/bin/env bash
set -uo pipefail  # 保留未定义变量和管道错误检查，但允许测试失败后仍写入 reward。
mkdir -p /logs/verifier  # 创建 Harbor 标准 verifier artifact 目录。
LOG_FILE=/logs/verifier/test-stdout.txt  # 固定记录完整官方测试输出的 Harbor 标准路径。
ERR_FILE=/logs/verifier/test-stderr.txt  # 固定记录 verifier 内部诊断的 Harbor 标准路径。
F2P_PATH='/testbed/pandas/tests/groupby/methods/test_sample.py'  # 保存唯一官方 FAIL_TO_PASS 测试文件绝对路径。
P2P_PATHS=()  # 初始化官方 PASS_TO_PASS 测试路径数组。
P2P_PATHS+=('/testbed/pandas/tests/indexes/multi/test_compat.py')  # 保存官方 PASS_TO_PASS 文件路径。
P2P_PATHS+=('/testbed/pandas/tests/series/methods/test_duplicated.py')  # 保存官方 PASS_TO_PASS 文件路径。
P2P_PATHS+=('/testbed/pandas/tests/io/pytables/test_put.py')  # 保存官方 PASS_TO_PASS 文件路径。
P2P_PATHS+=('/testbed/pandas/tests/io/excel/test_xlrd.py')  # 保存官方 PASS_TO_PASS 文件路径。
P2P_PATHS+=('/testbed/pandas/tests/indexing/multiindex/test_multiindex.py')  # 保存官方 PASS_TO_PASS 文件路径。
TEST_COMMAND_B64='cHl0ZXN0IC1yQSAtLXRiPXNob3J0IC0tY29sb3I9bm8gLS10aW1lb3V0PTEw'  # 保存经 base64 编码的官方 repo 测试命令。
TEST_TIMEOUT_SECONDS=600  # 使用官方 repo_settings 的单文件测试命令 timeout_run。
TEST_COMMAND=$(printf '%s' "$TEST_COMMAND_B64" | base64 --decode)  # 在 sandbox 内恢复原始官方测试命令。
source /opt/miniconda3/etc/profile.d/conda.sh  # 加载官方 FeatureBench Level-1 使用的 conda shell 函数。
conda activate testbed  # 激活官方实例镜像中的 testbed 环境。
cd /testbed  # 固定所有 Git 和测试操作在官方仓库工作树进行。
rm -f "$F2P_PATH"  # 删除 agent 可能重新创建或修改的隐藏 F2P 文件，保证使用官方 canonical 测试。
if ! git apply --reverse --whitespace=fix /tests/test_patch.diff >>"$LOG_FILE" 2>>"$ERR_FILE"; then  # 反向应用官方 test_patch 以恢复隐藏 F2P 测试文件。
  printf 'FeatureBench test_patch reverse apply failed\n' >>"$ERR_FILE"  # 记录协议失败原因。
  printf '0\n' > /logs/verifier/reward.txt  # 恢复失败时按官方 resolved 语义给零奖励。
  exit 0  # 正常结束 verifier，让 Harbor读取 reward 而非将其记为基础设施异常。
fi  # 完成隐藏 F2P 测试恢复分支。
run_target() {  # 用统一函数执行一个官方 F2P 或 P2P 测试文件并返回其退出码。
  local target_path="$1"  # 接收当前要运行的官方测试文件绝对路径。
  local quoted_target  # 声明安全拼接到 bash -lc 命令的临时变量。
  printf -v quoted_target '%q' "$target_path"  # 按 bash 语义转义当前测试文件路径。
  timeout "$TEST_TIMEOUT_SECONDS" bash -lc "$TEST_COMMAND $quoted_target" >>"$LOG_FILE" 2>>"$ERR_FILE"  # 按官方命令和 timeout 执行当前测试文件。
}  # 完成单目标测试函数。
F2P_OK=0  # 初始化 FAIL_TO_PASS 是否完全通过的布尔状态。
if run_target "$F2P_PATH"; then F2P_OK=1; fi  # 运行恢复后的唯一官方 F2P 测试文件。
P2P_OK=1  # 默认空 PASS_TO_PASS 列表为通过，和官方 harness 一致。
for p2p_path in "${P2P_PATHS[@]}"; do  # 逐个运行官方 PASS_TO_PASS 测试文件。
  if ! run_target "$p2p_path"; then P2P_OK=0; fi  # 任一 P2P 失败即保持整体失败。
done  # 完成全部 P2P 测试。
if [[ "$F2P_OK" == "1" && "$P2P_OK" == "1" ]]; then  # 仅 F2P 与 P2P 都通过才判定官方 resolved。
  printf '1\n' > /logs/verifier/reward.txt  # 写 Harbor 可解析的成功 reward。
else  # F2P 或 P2P 任一失败时进入零分分支。
  printf '0\n' > /logs/verifier/reward.txt  # 写 Harbor 可解析的失败 reward。
fi  # 完成二元 reward 写入。
exit 0  # 将测试结果通过 reward 而不是 shell 非零退出码交给 Harbor。
