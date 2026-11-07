# CLAUDE.md — DRC Copilot（PayPal AI Hackathon 2026）

> 给 VS Code 里的 Claude Code 读的项目说明。放在项目根目录 `drc-copilot/CLAUDE.md`。
> 作者：Celina（经济与管理背景，能读代码，主要靠 AI 写代码）。** 一律用英文。**

---

## 1. 项目一句话
**Turn PayPal merchants' transaction data into investable revenue-share assets — PayPal verifies, splits and services; outside capital funds.**
参考"每日收入分成（DRC / Revenue-Based Financing）"模式：投资者给小微商户出资，商户每天按已验证营业额的一定比例分成给投资者，累计回款达到封顶（cap）后合同自动终止。PayPal 作为基础设施：AI 验证流水、计算并执行每日分账、监测商户健康度。

- 比赛：PayPal AI Hackathon（Devpost），**截止 2026-11-12 12:00 PT（北京 11-13 04:00，德国 11-12 21:00）**
- 硬性要求：PayPal 必须是核心（钱真实在 PayPal 沙盒里流动）+ AI 有实质作用 + 可运行 Demo + 公开 GitHub（MIT）+ 3 分钟内 YouTube 视频 + 英文材料
- 市场设定：德国 / 欧盟的 PayPal 商户，币种 **EUR**

## 2. 演示角色（PayPal Sandbox，全部 DE / EUR）
| 角色 | 名称 | 类型 | 说明 |
|---|---|---|---|
| 商户 | Kaffee und Tee GmbH（新沙盒账户） | Business | **App 凭证属于这个账户**；顾客付款进这里；每日分账从这里打出 |
| 消费者 A | Anna Schmidt | Personal (DE) | 小额零散订单 |
| 消费者 B | John and Peter GmbH | Business | 咨询公司，大额、定期的团队餐/会议订单（真实交易，AI 不应误判为刷单） |
| DRC 托管账户 | DRC Fund I Escrow | Business | 接收每日分账；内部账本拆分为 PayPal 服务费 + 投资者净收益 |

账户邮箱、Client ID、Secret 全部放在 `.env`，**不得写进代码或提交到 Git**。

## 3. 分账规则（Split Engine，核心业务逻辑，改动需经 Celina 确认）
每日瀑布流（waterfall）：
1. `verified_net` = AI 清洗后（剔除规则确认的自刷单）、只计入已完成（`COMPLETED`）交易、并扣除 PayPal 收款手续费后的当日净额（真实交易用 Transaction Search 返回的实际 fee；模拟历史数据用 `config.FEE_MODEL` 估算）。**分账基数用 `verified_net`**；待复核（`at_risk`）的交易仍计入，单独汇总展示
2. `drc_due` = verified_net × 当前分成比例（阶梯：当日 verified_net 中不超过 Baseline Threshold 的部分 5%，超出部分 3%；Baseline Threshold = 融资前 90 天日均 verified_net 的 90%，签约时固定为 €703.26）
3. **Payouts 手续费由商户在分成之外额外承担（Celina 11/7 确认）**：`payout_amount = drc_due`（全额打给托管账户）；`payout_fee` 由 PayPal 另行向商户收取（实测 2%：€10 打款收 €0.20），记为商户的 `merchant_settlement_cost`，**不计入 cap、不从托管池扣**。商户当日总支出 = `drc_due + payout_fee`。以 PayPal 返回的实际 fee 为准记账
4. 托管资金池内部记账（不再单独打款）：
   ① PayPal 服务费（servicing fee，优先扣）→ ② 剩余为投资者净收益（不再有打款手续费这一项）
5. 累计 `sum(drc_due)` ≥ 合同封顶金额（cap）→ 停止分账，生成合同终止记录；当日只扣到刚好达到 cap
6. 显示三个指标（IRR 一律从投资人视角算，`payout_fee` 是商户成本，**不计入 IRR**）：
   - **gross IRR**：现金流 = 每日 `drc_due`（托管账户实际收到的钱）对比 `investment_amount`
   - **net IRR**：现金流 = 每日 `investor_net`（扣除 servicing fee 之后）
   - **merchant effective cost**（商户视角，另外展示），用来说明结算频率对商户的影响：
     - 总成本倍数 = `(累计 drc_due + 累计 payout_fee) / investment_amount`
     - **merchant effective APR** = 商户现金流的 XIRR：融资日 `+investment_amount`，之后每个结算日 `−(drc_due + payout_fee)`。用 numpy-financial 或二分法求解，和 IRR 的算法一致（numpy-financial 的 `irr` 只适用于等间隔现金流；按实际日期计算时，用二分法对 XNPV 求根）
7. **幂等**：`sender_batch_id = drc-{merchant_id}-{YYYYMMDD}`，同一天绝不重复打款（PayPal 会拒绝重复 ID，本地也要先查）
8. 结算频率可配置：每日 / 满额（如累计 ≥ €100）/ 每周 —— 由优化器根据手续费拖累给出建议

示例合同（`data/sample_contract.pdf`，数值以合同为准）：出资 €20,000，出资日 2026-11-06；分成 5%（不超过 Baseline Threshold €703.26/营业日的部分）/ 3%（超出部分）；封顶 1.15x = €23,000；最长 24 个月；服务费 5%；健康条款触发后降至 3%（最长 30 天，需投资人批准）；Baseline Threshold = 2026-08-08 至 2026-11-05 共 90 天 verified_net 均值 × 0.9

## 4. 系统闭环（模块）
```
① 收入进来     买家通过 PayPal 钱包 Send（商品或服务）付款给商户，paypal/reporting.py 用 Transaction Search 读取。（paypal/orders.py 保留：Orders Checkout 在德国沙盒被 COMPLIANCE_KYC_VIOLATION 拦截，代码可用于其他地区）
② AI 清洗      ai/anomaly.py         Isolation Forest 识别刷单/作废/改价/断流 → verified_net
③ AI 读合同    ai/contract_parser.py LLM 从 DRC 合同 PDF 提取结构化参数（JSON Schema）
④ 每日分账     engine/split_engine.py 按第 3 节规则计算 → paypal/payouts.py 真实打款
⑤ 收益追踪     engine/irr_tracker.py  累计回款、gross/net IRR、merchant effective cost、触达 cap 自动终止
⑥ 健康度 Agent ai/health_agent.py    移动平均+阈值 → LLM 生成风险报告 → 建议下调分成比例 → 人工批准后次日生效（回到 ④）
⑦ 资产报告     reports/asset_report.py 输出"已验证流水报告"（JSON + PDF）给投资者
（可选）       ai/optimizer.py        Monte Carlo 比较候选费率与结算频率
```
评委最看重的时刻：⑥ 发现风险 → 投资者点 Approve → 次日 ④ 的 PayPal 实际打款金额改变。

## 5. 技术栈
- Python 3.12，`requests`（直接调 PayPal REST，最稳）、`python-dotenv`、`pandas`、`numpy`、`numpy-financial`、`scikit-learn`、`pdfplumber`、`streamlit`、`streamlit-aggrid`、SQLite（`sqlite3` 标准库）
- 大模型：Claude API 或 DeepSeek API（`LLM_PROVIDER` 环境变量切换），合同解析必须用结构化输出并做字段校验
- 部署：Render（Web Service 跑 Streamlit）
- PayPal 接口：
  - Token：`POST /v1/oauth2/token`（Basic Auth，client_credentials）
  - Orders：`POST /v2/checkout/orders` → 买家批准 → `POST /v2/checkout/orders/{id}/capture`
  - Payouts：`POST /v1/payments/payouts`，查状态 `GET /v1/payments/payouts/{payout_batch_id}`
  - 对账：`GET /v1/reporting/transactions`（Transaction Search）
  - Base URL：`https://api-m.sandbox.paypal.com`

## 6. 目录结构
```
drc-copilot/
  CLAUDE.md  README.md  LICENSE(MIT)  requirements.txt  .env.example  .gitignore
  app.py                    # Streamlit 入口
  config.py                 # 读取 .env
  paypal/  client.py  orders.py  payouts.py  reporting.py
  ai/      anomaly.py  contract_parser.py  health_agent.py  optimizer.py
  engine/  split_engine.py  irr_tracker.py  ledger.py
  data/    generate_data.py  sample_contract.pdf  (生成的 csv 不提交)
  reports/ asset_report.py
  tests/   test_split_engine.py  test_irr.py
  docs/screenshots/
```

## 7. `.env.example`（真实值只放 `.env`）
```
PAYPAL_CLIENT_ID=
PAYPAL_SECRET=
PAYPAL_BASE_URL=https://api-m.sandbox.paypal.com
MERCHANT_EMAIL=
ESCROW_EMAIL=
CONSUMER_A_EMAIL=
CONSUMER_B_EMAIL=
LLM_PROVIDER=anthropic
ANTHROPIC_API_KEY=
DEEPSEEK_API_KEY=
```
`.gitignore` 至少包含：`.env`、`.venv/`、`__pycache__/`、`*.db`、`data/*.csv`

## 8. 编码规范（给 Claude Code）
- 先写小而能跑的版本，每完成一个模块就给出运行命令，让 Celina 在终端验证
- 金额一律用 `Decimal`，保留 2 位小数，币种显式传 `EUR`
- 所有 PayPal 调用集中在 `paypal/`，统一处理 token 缓存（过期前自动刷新）、错误（打印 `debug_id`）、重试
- 任何会动钱的操作（Payouts）前，界面必须有人工确认按钮，并显示将要打款的金额和收款方
- 不要擅自修改第 3 节分账规则；有疑问先问
- 解释代码时用中文、通俗、分步骤；改动后简述"改了什么、怎么测"
- 不引入未在第 5 节列出的重型依赖（如 LSTM、强化学习框架）

## 9. 开发计划（剩余天数）
| 日期 | 目标 | 完成标志 |
|---|---|---|
| 11/5 ✅ | 沙盒 4 账户 + Postman 测通 Token / Payouts / 状态查询 | 托管账户收到 €10，素材已存 |
| 11/6 ✅ | 建仓库；`paypal/client.py` + `payouts.py`；`generate_data.py` 生成 90 天流水（含异常）；`orders.py` / 买家 Send 付款 | 仓库已发布；payouts 自动打款 SUCCESS；generate_data.py 生成 90 天模拟历史流水（08-08 至 11-05，含 4 类植入异常）；买家 Send 4 笔真实付款（€171.50 gross / €166.84 net）；合同出资日定为 2026-11-06（Baseline 基线 = 08-08 至 11-05 共 90 天） |
| 11/7 | `anomaly.py`（交易级 IsolationForest + 规则，日级移动平均/渠道占比）、`make_contract.py` 生成合同 PDF、`contract_parser.py`（Claude tool use + source_quote + 校验）、`ledger.py` + `split_engine.py` + 单元测试 | 4 类异常按类型输出 precision/recall，John and Peter 订单误报≈0；合同 → `contract_params.json` 校验通过；11/6 净营业额 €166.84 → drc_due €8.34（5% × €166.84，低于 Baseline Threshold）全额打款至托管账户 SUCCESS（商户另付手续费约 €0.17），同日重复运行被拒绝 |
| 11/8 | `irr_tracker.py`、`health_agent.py`、`optimizer.py`（简版） | 触达 cap 自动停；风险报告生成 |
| 11/9 | Streamlit 4 页面 + "Simulate next day" 按钮 + AG Grid 表格 | 本地全流程可点 |
| 11/10 | 部署 Render、README、Devpost 文案 | 公开链接无痕窗口可用 |
| 11/11 | 录视频、上传 YouTube、提交 Devpost | 提交成功（11/12 为缓冲） |

## 10. 已知事实（实测）
- 沙盒 Payout 约 24 秒从 PENDING → SUCCESS
- 打款手续费 2%（€10 → €0.20），由付款方承担；收款方 Fee = €0.00
- 每次打款在商户记录里生成两行：Mass payment（批次）+ Payment to 收款人
- 交易记录页可导出 CSV
- 德国沙盒里 Orders API 的 Checkout 页面被拒（`COMPLIANCE_KYC_VIOLATION`，换买家、换商户都一样；已排除账号、App、付款来源、订单写法），所以**顾客付款改用买家端 Send → Goods and Services 进入商户账户，代码通过 Transaction Search（`paypal/reporting.py`）读取**；`orders.py` 保留为代码，演示时如实说明。卡支付路径（`--mode card`）同样不通：`PAYEE_NOT_ENABLED_FOR_CARD_PROCESSING`（商户账号未开通卡处理，App 里勾选无效）
- 商户收款费率实测 1.90% + €0.35/笔（€145 → €3.11）
- Payout 状态会经过 PENDING → PROCESSING → SUCCESS，**只有 SUCCESS 才记账**
- **待办**：合同按营业日（Trading Day）计算。接入真实数据时，`anomaly.py` 的 7 日 / 30 日均线要改为按 Trading Day 计算，未事先通知休息的零交易日按 0 计入（目前模拟数据每天都有交易，按自然日算暂无差别）
