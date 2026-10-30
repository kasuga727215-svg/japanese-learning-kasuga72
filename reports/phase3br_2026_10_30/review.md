# Phase 3B-R：分類契約、有效詞庫與歷史去重

審查日期：2026-10-30，Asia/Taipei。

## 結論與驗收邊界

**CONFIRMED：已在隔離環境修復有直接詞典證據的分類契約問題，沒有修改正式資料。**

**NOT VERIFIED：正式 N5 剩餘庫存、244 筆完整分類、374 筆歷史命中逐筆稽核，以及正式剩餘庫存能否支援未來 7／30 日。** 未取得正式 PostgreSQL 唯讀角色或逐筆快照，不宣稱正式故障已解除。隔離來源容量與正式剩餘容量是不同驗收。

本次不 Commit、Push、Merge、建立 PR 或部署；不呼叫正式 Gemini。正式資料寫入 0、正式 Gemini 呼叫 0。未新增正式或應用程式 DB schema，未修改詞庫分類或 Bank 資料。

## 1. 版本證據

| 項目 | 本次唯讀核對 |
|---|---|
| Render Live | `69207c261d84a0fdb21f967a68cfa2e643713951` |
| Render Deploy | `dep-dbekdlivcj2c73d8hnl0` |
| Render 連結分支 | main |
| Auto-Deploy | Off，未更動 |
| GitHub 修復分支 | `codex/selector-resumable-pagination-2026-10-24`，69207c2 |
| GitHub main／本機 HEAD | `1b9fafe98830de329fa62eeeba108137686f39c3` |
| 修改前工作區 app.py | Git clean blob `c63c6255ad181823d2790365ec19279ab6a47fef`，符合 69207c2 |

工作區原本就有 10/24 修復的未提交／未追蹤檔案，以及舊審查報告與不相關的 `main` 檔案。本次保留它們，沒有使用舊 HEAD 覆蓋。用 GitHub blob 唯讀取得 69207c2 的基準副本，放在 ignored 的 `tmp-starter-db/phase3br-baseline`，本次差異以此基準產生，不以舊 main 的大型 diff 冒充本次修改。

## 2. 共同根因與以前尚未完成的項目

| 階段 | 已有處理／仍存在的問題 |
|---|---|
| Phase 1／2 | 修正自由生成重複、分包、部分完成、reserve、編碼與 request 時間問題；這些是流程／儲存層修復，不能證明詞性正確。 |
| 10/24、69207c2 | SQL 來源過濾、ID-only、安全 hydration、keyset、可續跑、materials-only history 已發布；本次保留。 |
| 10/25／26 | 正式 8／5／5 成功，但 Word 收到 あびる／かける 後被 Gemini 拒絕；證明數量成功不等於分類契約正確。 |
| Phase 3A | 只審查，未修復／部署：最佳分詞路徑歧義、Word unknown POS 放行、grammar STOP 的 malformed JSON 被稱為 truncated、舊 cache identity mismatch。 |
| 10/30 | N5 已掃完，但只證明「現有分類器找不到已驗證且未排除候選」。不能把 244 筆未驗證都說成非動詞，也不能把 1950 詞彙當成 1950 動詞。 |

核心是分類契約分散：動詞只看最佳分詞路徑，Word 卻接受該路徑的人名／泛用 POS；動詞驗證失敗又直接建議 word。歷史使用增加後，分類器漏認與來源容量問題開始影響正式配額。**尚不能證明正式庫存真正耗盡。**

grammar JSON 錯誤命名與 cache display identity 問題，本次未修改 compact_json 或 Bank 主流程，仍屬後續審查事項；已有同級 grammar reserve 保護且本次回歸保留。

## 3. 10/30 正式 N5 分層數字

Render 10:08:51 原始記錄（qsztc），Job `92b4532ee1844e7dbd3d47f6293ecbb5`：

| 第一個排除／結果 | 筆數 | 證據狀態 |
|---|---:|---|
| 原始 inventory | 1950 | 使用者 LOG；排除總和亦吻合 |
| SQL category | 1281 | CONFIRMED，Render log id `0499d1bf-765e-4618-95b6-2191029ea5de` |
| 進入 hydration／Python 掃描 | 669 | CONFIRMED，`c28e4e6c-cdb7-43ac-9238-04a94794f182` |
| history 或 current batch | 374 | CONFIRMED 為合併計數，未取得逐筆命中 |
| not_verified_dictionary_verb | 244 | CONFIRMED 舊分類器計數，不代表 244 筆非動詞 |
| variant_separator | 11 | CONFIRMED 計數，實際詞條未取得 |
| known_non_verb | 4 | CONFIRMED 計數，實際詞條未取得 |
| counter_or_number | 36 | CONFIRMED 計數，實際詞條未取得 |
| selected | 0 | CONFIRMED，`67d463d6-acb4-440e-bcb0-cee8ba6d66e7` |

`1281+669=1950`；`374+244+11+4+36=669`。程式 SQL diagnostics 使用 first-match CASE，Python rejection 也先回傳第一個原因，因此統計設計為互斥桶。但未取得 row id 集合，**實際集合互斥性與分類合理性仍 NOT VERIFIED**。

N5 此次 elapsed 296.12 ms、bad_rows=0；這次不是 N5 Selector timeout。Render 同時記錄 `job_failed_before_enrich gemini_call_count=0`（`f1f860d8-fefc-48e2-b9d2-fbe49eaa280d`），前置消費防護已生效。

### 1281 筆 category 的來源

CONFIRMED 程式排除的 category 包括 business、advanced、generated_compound、unknown、named_entity、proper_noun、person_name、place_name、company、brand、legal、medical、finance、financial、archaic、classical、specialized、technical、sensitive、typo_or_noise。source 排除 synthetic、auto_generated、generated、seed_advanced_synthetic。

本次沒有放寬 SQL 過濾。程式所用的 pool schema 沒有依 `item_type` 分表／分欄選取；item_type 是 selector 的用途參數，source/category 與 POS 是不同層。

SUSPECTED：舊本地稽核恰有 N5 business=651、advanced=630，合計 1281；例為 業務改善、業務管理上、業務分析化 等 seed_advanced。**本地同數量不能證明正式 1281 筆就是同一集合。** importer 遇相同 normalized_key＋level 會 skip、不覆蓋舊分類，因此來源重疊保留舊 category 是值得核對的風險，但不能未經逐筆核對就修改正式 category。

是否有合法 N5 動詞被這 1281 筆誤殺：NOT VERIFIED。

### 244 筆與 374 筆

尚無正式逐筆輸入，不能列出真實的 244 筆分析結果，也不以 CSV 湊數。完整操作計畫在 `readonly_audit_plan.md`。

10/30 正式 history LOG 是 **1169 個 key、151 份 materials**，word／verb 各次讀取一致，並非先前不同案例的 3237。程式只讀 materials 的已存 word／verb 主詞條，去掉顯示附註再做 NFKC/lower；不加入 reading、中文、例句、bank used_count、失敗 job 或額外 recent 黑名單。

374 是 history/current batch 合併桶，而且先於 POS 檢查；其中可能包含本來就不是動詞的詞。不能計算 `669-374` 後把剩下全算成動詞，也不能把 374 全當成用過的動詞數。

現行 history 為 word＋verb 的 canonical surface union，不區分 sense；表記不同不自動用讀音合併。可能的同形異義、跨 item_type、顯示附註碰撞仍需逐筆稽核。**本次不擅自放寬或重新定義 history。**

## 4. 實際詞典根因與修正

CONFIRMED，使用正式 requirements 中固定的 MeCab／UniDic-lite 版本：

- あびる：最佳路徑為人名姓氏；同一 lattice 有完整 動詞・上一段／浴びる／orthBase=あびる。
- かける：最佳路徑為人名；lattice 有 掛ける、駆ける、欠ける 等完整動詞詞條；不根據同音讀音任意替換詞條或義項。
- だます：最佳路徑拆成 だ＋ます 助動詞；lattice 本身已有完整 五段動詞／騙す／orthBase=だます，不需要固定別名特例。
- 向こう：普通名詞成立；其他動詞節點是 向く 的意志推量形或 向かう 的連用形，不是字典形。
- 遠く：有副詞可能名詞；不是動詞。或る 是連體詞，こんにちは 是感動詞。
- 不便：UniDic-lite 標為 名詞・普通名詞・形状詞可能，不是要求所有字典都用同一個「形容動詞」標籤。

新共用介面 `classify_lexeme` 回傳 `verified_verb / verified_word / unknown / rejected_invalid`，附 reason、實際 entry evidence、dictionary/source version、classifier_version、source_fingerprint、retryable、verification_route。

分類規則：

1. 查 exact surface 的全部已知詞典 entry，不只最佳分詞路徑；動詞仍須真正字典形活用、orthBase 相符，不接受單純字尾。
2. 不把 lemma、orthBase、reading 當成同一欄。reading 只能縮小 exact-entry 的證據，不能以同音替換詞條。
3. する複合保留已知名詞＋サ変可能詞幹及字典形する證據；不因後綴就放行任意字串。
4. POS 別名共用同一判定；形容動詞仍是 Word，五段動詞／動詞（自動詞）不能因 unknown default 變成 Word。
5. 同時有普通 Word 與 Verb 義項而缺少可解歧來源時回 unknown；例如 ある／かかる。可信來源 POS 與已存在的詞典 entry 可解歧；來源與詞典衝突也回 unknown，不直接認定非動詞。
6. proper-noun-only 與資料格式無效另外分類；沒有新增固定詞黑名單。
7. 候選 cache 保留來源 reading／POS／分類證據，避免 Phase A 與 enrich 重驗遺失上下文。Gemini prompt、compact JSON 格式及模型設定完全不變。

Unknown 在 Selector state 保存 id；分類器版本納入 cursor context，可在版本變更時失效重驗。掃描完成但尚有 unknown 且正式 primary 不足，回 `classifier_uncertainty`，不是 `candidate_pool_exhausted`。Unknown 不寫入 materials used，也不自動 suggested_type=word。沒有建立正式持久化黑名單或新增正式表。

## 5. 修復前後分類矩陣

僅限 14 個具備逐項詞典證據的案例，不是對正式 244 筆或整個 pool 的人工 gold 驗證。

| 真實／可驗證狀態 | 舊結果 | 新結果 |
|---|---|---|
| 7 個合法動詞 | 5 verb、2 誤收 word | 7 verb |
| 6 個合法非動詞詞條 | 6 word、0 誤收 verb | 6 word、0 誤收 verb |
| 1 個無完整詞條證據的片語 | 1 直接當 word | 1 unknown |

有證據的動詞漏認 2→0，誤收為 Word 2→0。未對未取得的正式候選宣稱全體誤殺率為零。

## 6. 本地有效來源盤點與容量

以下是 checked-in OpenJLPT，保留原 reading／level、依既有 normalized surface 去重、**初始隔離歷史為空**，不是正式剩餘庫存。N5 CSV 只有 662 筆（163 筆 reading 空白），並非正式 N5 1950 或 scanned 669，不能互相替代。

| Level | verified_word | verified_verb | unknown | rejected_invalid |
|---|---:|---:|---:|---:|
| N5 | 454 | 105 | 102 | 1 |
| N4 | 375 | 156 | 96 | 3 |
| N3 | 1394 | 224 | 89 | 2 |
| N2 | 1253 | 373 | 154 | 5 |
| N1 | 2549 | 477 | 271 | 6 |

逐筆出處、行號、原 reading／meaning、四態 evidence 在 `after.json`，舊分類對照在 `before.json`。這些數量是 source surface 候選，不是經人工逐義項合併後的獨立語彙數；`summary.json` 另列可能異表記碰撞，未擅自合併或修改 history。

### 隔離逐日消耗

測試直接從 CSV 寫入隔離 pool → 真正 Selector → primary/reserve/refill → 離線 Gemini 回應 → finalize → save → reload。只 mock Gemini 回應，不 mock 候選庫存、不額外創造補足詞條，不重用歷史、不跨級補正式 quota。

實際執行的測試結果與各日各級前後庫存，以 `summary.json`、`lexical_tests.json`、`daily_inventory.csv` 為準。最終結果：

| 來源與 DB | 全等級 7 日 | 全等級 30 日 | 每日結果 |
|---|---|---|---|
| CSV 保留 reading → SQLite | 7／7 PASS | 30／30 PASS | 8／5／5，fallback=0 |
| 相同來源 → 真正隔離 PostgreSQL17 | 7／7 PASS | 30／30 PASS | 8／5／5，fallback=0 |
| 正式 PostgreSQL18 剩餘來源 | NOT VERIFIED | NOT VERIFIED | 未執行正式生成 |

兩種隔離 DB 的有效動詞庫存一致（現行 source-surface identity）：

| Level | 初始 | 第 7 日存檔後 | 第 30 日存檔後 | 尚未確認候選（未當動詞使用） |
|---|---:|---:|---:|---:|
| N5 | 105 | 98 | 75 | 102 |
| N4 | 156 | 149 | 126 | 96 |
| N3 | 224 | 217 | 194 | 89 |
| N2 | 373 | 366 | 343 | 154 |
| N1 | 477 | 470 | 447 | 271 |

全等級固定 Word 2／2／2／1／1、Verb 每級 1、Grammar 每級 1；每次成功必須 8／5／5、fallback=0、bank_fallback_allowed=false。每天只在 save 成功後更新隔離 history。

既有 N5-only 30 次情境不是 30 次成功：21 日成功，後 9 次 Phase A 停止、沒有追加 Gemini；新分類將剩餘未確認候選正確回報 classifier_uncertainty，而不是假裝已證實來源全部耗盡。這個配置的 30 日容量驗收未通過。全等級與 N1-only 的三日／三十日是不同配置，分別記錄，不能用來替代 N5-only 結論。

## 7. 回歸與未改範圍

本次重新執行原先 18 個 generation 測試及 23 個 SQLite／真實隔離 PostgreSQL selector 測試；原套件合計 38 PASS、3 SKIP。新增四態、表記與來源義項、unknown 非永久拒絕、缺 reserve 仍保留 primary、來源證據 cache roundtrip，以及 SQLite／PostgreSQL 各 7／30 日逐日消耗測試。最終數字見機器產出的 summary；不是正式 Render PASS。

新增完整套件 22 PASS，最後新增／加強的 10 項 targeted check 也全部 PASS，其中 8 項與完整套件重疊、2 項是 Gemini 未提供 suggested_type 時不能預設 word 的新增 E2E。新增不重複案例合計 24 項，不能把兩次執行數直接相加當成 32 個不同案例。

第一次加強 POS assertion 時曾錯把 不便 的 UniDic 大類預期成 形状詞，實際證據是 名詞＋形状詞可能；依實際欄位修正測試後重跑 PASS，沒有為了配合錯誤測試改寫詞典證據。早期測試未保留 CSV reading 的容量結果亦已作廢，以本次保留來源 reading 的完整重跑為準。

SKIP 的三項有明確原因：PostgreSQL 本身拒絕插入非法 UTF-8 text；模擬 driver decode fault 是 SQLite-backed 故障注入；CSV history 特例由 SQLite 覆蓋，PostgreSQL 另跑真正 materials table 的 E2E。不得稱這三項在 PostgreSQL PASS。

使用真正隔離 PostgreSQL 17.11，僅綁定 127.0.0.1:55433、臨時 schema；不是正式 PostgreSQL 18。正式版本差異仍須後續唯讀／另授權實測。

已保留：SQL source/category 過濾、ID-only、byte-safe hydration、壞 row 隔離、keyset continuation、micro-step、Phase A 零 API、materials-only history、bank completion cache、同級 primary/reserve/refill、grammar rotation、固定 quota。

AST hash 驗證未改：`call_gemini`、`build_gemini_daily_enrich_prompt`、`prepare_all_gemini_daily_fresh_candidates`、`finalize_gemini_generation_job`、`save_material_for_date`、`api_materials`、`gemini_bank_material_history_keys_for_item_type`。也沒有修改 settings、UI、Telegram、quiz、錯題、動詞練習、文法拆解或 fallback 設定。

## 8. 修改檔案與函式

根目錄：`C:\Users\88698\Documents\Codex\2026-05-22\vs-code`。

- `app.py`：POS adapter、audit_verb_candidate、classify_daily_fresh_pool_row、Word predicate／enrich POS guard、candidate payload 的來源證據、Selector metadata／cache／錯誤分類。daily_fresh runner 只改 unknown rejection 的 suggested_type log 預設，不改狀態機或模型呼叫。
- `services/verb_candidate_audit.py`：exact lattice lookup、共用 POS 契約、四態分類與相容 wrapper。
- `services/candidate_selector.py`：僅增加 unknown id／統計／錯誤區分；SQL、hydration 與 keyset 主架構不變。
- `scripts/test_daily_fresh_generation.py`：保留 CSV reading，區分真正耗盡與 classifier uncertainty 的情境結果。
- `scripts/test_selector_scan_regression.py`：未解析合成 fixture 須回 uncertainty，不能誤報來源耗盡。
- 新增 `scripts/test_lexical_contracts.py`、`scripts/audit_lexical_contracts.py`。
- 本資料夾的報告、逐筆 evidence、每日庫存表與相對 69207c2 的 `repair.patch`。

## 9. 新詞源與品質方案

目前不能判定正式 N5 是否需要新增詞源；須先完成 244／374／1281 逐筆稽核。即使有大量一般詞彙，也不等於有足夠未用 N5 動詞。

建議先以 [JMdict 官方資料](https://www.edrdg.org/wiki/index.php/JMdict-EDICT_Dictionary_Project) 作為 POS／表記／義項的交叉證據，保留 entry id、sense id、reading restriction、POS、版本與雜湊，再與既有 OpenJLPT level 對齊；**JMdict 不會自動提供可信的 N5 分級**。詞源授權依 [EDRDG 原始條款](https://www.edrdg.org/edrdg/licence.html) 完成署名與版本更新規劃，未下載／匯入正式資料。

UniDic 的 lemma、orthBase、reading 並不等同，依 [國語研欄位說明](https://clrd.ninjal.ac.jp/unidic/faq.html) 留下可追溯證據；lattice alternate lookup 使用 [MeCab 官方介面](https://taku910.github.io/mecab/doxygen/classMeCab_1_1Lattice.html)。不以同音字自動合併義項。

[JLPT 官方 FAQ](https://www.jlpt.jp/sp/e/faq/) 不提供新版完整詞彙表，因此新的 N5 label 不能自稱官方認證。新增來源須有可追查的分級出處、日常自然度／義項與 POS 複核、授權審查、隔離 dry-run、與歷史交集統計，再由使用者核准資料增補。不自行新增未驗證 N5 詞條。

## 10. 待授權事項與剩餘風險

1. 正式 SELECT-only 快照：逐筆確認 1281／244／374、五級未用有效動詞與未知數。
2. 若要跨日保存分類證據／cooldown，先審查獨立 evidence 表、版本失效與 rollback；提案見 readonly_audit_plan.md，未建表。
3. 若真正 N5 有效來源不足，先核准詞源／分級／義項，再做資料增補；不能靠 reserve 放寬或歷史重用。
4. homograph／異表記 history 策略需有逐筆證據與獨立契約決定，不能在本次默默改變。
5. 真實 Gemini 回應品質、正式 PG18 及正式長期容量仍 NOT VERIFIED；本次未授權部署或生成。
6. grammar malformed JSON 的錯誤名稱與 cache identity mismatch 尚未修復，保持同級備援，不把它們宣稱已解決。
7. Unknown 目前保留於 job state 與可重現的隔離 evidence；沒有正式持久化審核佇列，後續仍需人工或可信字典證據解除不確定性。

## 11. Rollback

正式環境沒有部署變更，不需執行 Render rollback，也沒有資料庫回復操作。

本機不要 `git restore app.py` 到舊 main，否則會丟掉 69207c2 的既有未提交修復。Windows 工作區 core.autocrlf=true，因此人工審查後先執行 `git apply --reverse --check --ignore-whitespace reports/phase3br_2026_10_30/repair.patch`，確認沒有後續修改衝突，再決定是否 reverse 本次 patch；本次只執行 check、沒有回退。報告與測試證據可保留。若之後已有其他修改，逐段反向套用，不覆蓋無關工作。

**本次交付是具證據的本地修復與隔離驗證，不是正式 N5 庫存或正式 Render 長期容量驗收通過。**
