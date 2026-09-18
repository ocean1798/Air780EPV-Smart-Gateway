# 合宙 780 工程工作区规范整改与临时文件回收清理方案 (吸收审查意见修订版)

## 1. 背景与违规现状核查

经严格核对工作区总则（`AGENTS.md`）与《Project 文件合同》（`contract/project-files.md`），当前确认存在以下违规行为与产物：

1. **根目录越界污染（严重违规）**：
   - **对象**：仓库根目录下非法生成了 `specs/` 目录，内含 63 个 `.png` 截图文件。
   - **成因**：执行自动化 Playwright 脚本截屏时，直接使用了 `'specs/*.png'` 相对路径，而在根目录启动时产物未定向至项目子目录。
   - **违背规约**：“根目录仅保留职责确实要求位于根的共享入口、配置等文件。任务产物及工具副产物在写入前确定归属和实际输出位置，不因当前工作目录在根就默认写到根”。

2. **项目根目录平铺散落一次性临时脚本与中间原型（中度违规）**：
   - **对象**：`projects/02work/20260909-合宙780系列模组开发/` 根目录下遗留了：
     - 3 个一次性文本/样式修补脚本：`apply_full_colloquialization.py`, `build_perfect_production.py`, `build_production_release.py`；
     - 14 个过程验证与截图脚本：`capture_*.py` (9个), `verify_*.py` (5个)；
     - 5 个中间原型 HTML：`prototype_accordion_feed.html`, `prototype_adaptive_gateway.html`, `prototype_drawer_list_architecture.html`, `prototype_single_row_feed.html`, `prototype_ultimate_adaptive.html`。
   - **违背规约**：“收尾将本次产生、之后用不上的临时文件移入系统回收站”。这些中间工具的产物已固化至生产代码（`tools/cluster_gateway/web/index.html` 等），原脚本与原型属于不再需要的临时过程文件。

3. **测试资产未收敛至规范子目录（轻度违规）**：
   - **对象**：15 个自动化测试脚本（`test_*.py`）直接平铺在项目根目录。
   - **违背规约**：依据《文件合同第 1 节》，项目级测试资产与验证证据应归属在项目 `test/` 目录中。

4. **工具脚本未归入 tools/ 目录（轻度违规）**：
   - **对象**：`auto_flash_air780ec.py`（Air780EC 烧录监视工具）直接平铺在项目根目录。
   - **违背规约**：运维烧录工具应归属于 `tools/Luatools/`，不应混入根目录或纯测试目录。

---

## 2. 拟采取的治理与整改方案

### 2.1 规则守则与安全防灾前置要求
- **删除统一使用系统回收站**：严格调用 Python `send2trash` 模块，**严禁使用任何物理永久删除（os.remove, rm, del, Remove-Item 等）**；
- **防灾与异常阻断机制**：
  1. 必须在执行前脚本显式断言 `import send2trash` 成功；
  2. 遇到任何文件占用（PermissionError / Windows 文件锁）等异常时，**必须保留原对象并立即阻断报告原因，绝对不得自动改用永久删除**；
  3. 执行严密时序防护：先迁移留存证据 -> 逐个回收剩余临时文件 -> 校验源目录确已为空 -> 移入空目录本身；
  4. 严禁清空回收站。

---

### 2.2 具体分类处置清单

#### 分类 A：最终有效验收证据迁移（迁入项目合规 `evidence/` 目录）
依据《Project 文件合同》，`specs/` 仅用于承载长效技术事实正文，测试截图证据必须存放在 `evidence/`。将以下 7 个具有版本验收里程碑价值的最终证据图从根目录 `specs/` 移动至 `projects/02work/20260909-合宙780系列模组开发/evidence/`：
1. `specs/air28_live_verified_1280x820.png`（AIR-28 桌面端最终视效证据）
2. `specs/air28_live_verified_1417x875.png`（AIR-28 大屏端最终视效证据）
3. `specs/air28_live_verified_360x812.png`（AIR-28 移动端最终视效证据）
4. `specs/air29_tested_slot2_modal_v128.png`（AIR-29 升级弹窗最新基准证据）
5. `specs/air29_tested_slot2_v128_desktop.png`（AIR-29 固件升级后桌面基准证据）
6. `specs/air29_v127_live_success_1280x820.png`（AIR-29 升级成功状态基准证据）
7. `specs/air29_v127_modal_latest.png`（AIR-29 真实元数据拉取证据）

#### 分类 B：安全移入回收站（彻底清除根目录违规 `specs/`）
- 根目录 `specs/` 剩余的 56 张中间裁切、探针与调试图，在分类 A 迁移确认完成后，逐一通过 `send2trash` 移入系统回收站；
- 确认根目录 `specs/` 为空后，将根目录 `specs/` 文件夹本身通过 `send2trash` 移入回收站；
- **清理后预期**：仓库根目录彻底无 `specs/` 目录。

#### 分类 C：安全移入回收站（项目根目录一次性临时过程文件）
以下 22 个已无维护价值的临时修补脚本、过程截图脚本与中间 HTML 原型，逐一调用 `send2trash` 移入系统回收站：
1. `apply_full_colloquialization.py`
2. `build_perfect_production.py`
3. `build_production_release.py`
4. `capture_4carriers_evidence.py`
5. `capture_accordion.py`
6. `capture_accordion_scroll.py`
7. `capture_air22_visual_evidence.py`
8. `capture_new_arch.py`
9. `capture_prototype.py`
10. `capture_single_row.py`
11. `capture_single_row_mobile.py`
12. `capture_ultimate.py`
13. `verify_4carriers_render.py`
14. `verify_air15_disconnect_and_refresh.py`
15. `verify_perfect_matched.py`
16. `verify_restored_production.py`
17. `verify_web_cluster_ui.py`
18. `prototype_accordion_feed.html`
19. `prototype_adaptive_gateway.html`
20. `prototype_drawer_list_architecture.html`
21. `prototype_single_row_feed.html`
22. `prototype_ultimate_adaptive.html`

#### 分类 D：工程烧录工具归入 `tools/`
将硬件烧录监视工具移至专职目录：
- `auto_flash_air780ec.py` -> `projects/02work/20260909-合宙780系列模组开发/tools/Luatools/auto_flash_air780ec.py`

#### 分类 E：测试资产归档至 `test/` 并重构路径引用
在项目内新建目录 `projects/02work/20260909-合宙780系列模组开发/test/`，将以下 15 个核心测试用例脚本移动归档至 `test/`：
1. `test_air17_push_ack.py`
2. `test_air18_call_fota_e2e.py`
3. `test_air21_firmware_flasher_suite.py`
4. `test_air22_cluster_gateway_suite.py`
5. `test_dual_dongle_probe.py`
6. `test_mcp_cluster_live.py`
7. `test_no_hardware_stability.py`
8. `test_session_pool_live.py`
9. `test_silicon_plugin.py`
10. `test_sms_lazy_load_live.py`
11. `test_state_change_notify.py`
12. `test_web_cluster_live.py`
13. `test_web_console.py`
14. `test_webhook_config.py`
15. `test_zero_traffic_live.py`

**路径引用重构要求（彻底消除破坏性路径断裂）**：
- 在 `test/` 目录下创建 `__init__.py` 与引导模块；
- 对依赖 `PROJECT_ROOT`、`BASE_DIR` 的脚本（如 `test_web_console.py`, `test_air21_firmware_flasher_suite.py`, `test_air22_cluster_gateway_suite.py` 等），将其原先从当前目录获取项目的逻辑重构为二级父目录：
  ```python
  # 统一适配子目录结构：
  BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
  ```
  确保定位到正确的 `tools/host_gateway`、`tools/cluster_gateway` 与 `deploy/`。

#### 分类 F：同步修复 Change 中的证据引用
核查并更新 `changes/0028-实施-自适应网关UI与去工程化交互/tasks.md` 等相关文档中对已移动截图的引用路径，将 `specs/air28_live_verified_*.png` 修正为 `evidence/air28_live_verified_*.png`，消除 Markdown 潜在死链。

---

## 3. 验收标准与验证流程

1. **根目录合规核验**：
   - 执行 `ls -la` 验证根目录不存在 `specs` 目录，无任何未跟踪的多余临时文件；
2. **项目目录合规核验**：
   - `projects/02work/20260909-合宙780系列模组开发` 根目录仅保留合规常设文件与标准目录；
3. **测试用例编译与离线执行验证**：
   - 执行 `python -m py_compile projects/02work/20260909-合宙780系列模组开发/test/*.py` 确保全量语法与编译 100% 通过；
   - 运行纯离线单元测试套件：
     ```bash
     python projects/02work/20260909-合宙780系列模组开发/test/test_air21_firmware_flasher_suite.py
     ```
     确保路径重构后测试套件完全绿灯通过；
4. **黑板合同校验**：
   - 执行 `python .agents/skills/lead-control/scripts/validate_boards.py` 必须保持 13 个黑板 100% PASS。
