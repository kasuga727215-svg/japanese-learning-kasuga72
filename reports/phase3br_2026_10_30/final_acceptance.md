# Phase 3B-R 發布前最終驗收

日期：2026-10-30。結論：**NOT READY，不能宣稱正式 10/30 故障及長期容量已解除。**

本次保留現有 Phase 3B-R 修復，只新增隔離驗收工具與報告。正式資料寫入 0、正式 Gemini 呼叫 0；沒有 Commit、Push、Merge、PR 或部署。

## A. 已確認的共同根因

**CONFIRMED**：69207c2 的舊分類器只採用 MeCab 最佳路徑。あびる、かける 的最佳路徑可能是人名，但同一 lattice 有 exact surface 的合法字典形動詞。舊 Word 與 Verb 規則不對稱，動詞驗證失敗又可能被當成 Word。

**CONFIRMED**：現有修復實際查詢完整 lattice，並用共用四態結果區分 verified_verb、verified_word、unknown、rejected_invalid。已改善上述兩筆分類與 Word/Verb 衝突，保留材料歷史去重及固定等級配額。

**CONFIRMED**：分類器找到的有效候選、真正來源容量、已保存歷史排除是三個不同集合。Unknown 不能視為確認非動詞，也不能計入已驗證可供應量。

**NOT VERIFIED**：正式 1281 筆 category 排除的逐筆合理性、244 筆 not_verified 的真實分類、374 筆 history/current batch 的逐筆碰撞，仍未取得正式候選池資料。

## 版本與保護

- 使用者指定已部署基準：69207c261d84a0fdb21f967a68cfa2e643713951。既有 review.md 保存其 Render 查核證據；本次沒有部署或改動 Render。
- 本次重新讀取本機分支為 main，HEAD 為 1b9fafe98830de329fa62eeeba108137686f39c3，暫存區為空。不能拿此舊 HEAD 覆蓋現有修復。
- repair.patch 對現有修復執行 reverse check 成功，僅檢查，沒有套用或撤回。
- app.py、services/verb_candidate_audit.py、services/candidate_selector.py 本次前後 SHA256 相同，值在 final_acceptance_summary.json。
- 原回歸包含七個保護函式 AST hash 一致檢查：Gemini 呼叫、enrich prompt、Phase A、finalize、save_material_for_date、api_materials、materials history reader。
- Keyset、ID-only、安全 hydration、可續跑 micro-step、Phase A 零 API、Bank cache、同級 reserve/refill、grammar rotation、儲存契約未重新實作。

## 1. 24 個新增測試到底測了什麼

本次重新執行 12 個測試方法，分別在 SQLite 與隔離 PostgreSQL 測試類執行，共 **24 次 PASS**。不是 24 種不同端到端流程，也不是 PostgreSQL 類中的每個方法都會執行 SQL。

| 方法 | 實際路徑與邊界 |
|---|---|
| attested_verbs_not_suffix_guesses | 真正共用分類器、MeCab/UniDic、Word predicate、verb audit；七個具體正例，非完整詞庫 gold。 |
| nonverbs_keep_their_attested_senses | 真正詞典分類、POS evidence、Word/Verb guard；固定反例，非 E2E。 |
| unknown_not_suggested_word_and_reading_is_not_identity | 真正分類器、來源 POS 解歧與 reading 限制；含人工不明字串。 |
| pos_contract_does_not_accept_unknown_or_verb_aliases_as_word | 真正 POS adapter；固定 POS 標籤單元測試。 |
| unknown_selector_keeps_ids_without_false_exhaustion | 隔離池中的未知詞經真正 selector、state、Phase A；確認 classifier_uncertainty 且零模型呼叫。 |
| missing_reserve_does_not_change_primary_quota | 真正 selector 與 primary/reserve contract；隔離資料刻意縮成一詞，再測真正耗盡。 |
| candidate_cache_preserves_source_sense_evidence | 真正分類器及 candidate builder，但原測試僅 JSON roundtrip，不是 DB 持久化驗證。 |
| gemini_rejection_without_suggested_type_stays_unknown | 真正 job/selector/enrich/parser/replacement/finalize/save；注入一次離線 Gemini rejected。 |
| same_level_rejected_verb_and_truncated_grammar | 真正流程；注入 N5 verb rejected、N4 grammar truncated，確認同級補位。 |
| protected_functions_unchanged | 讀取實際程式 AST hash，非生成測試。 |
| capacity_seven_days / capacity_thirty_days | CSV 進真實 pool → selector → job/cache → 離線模型回應 → parser → bank → finalize → save/reload；逐日累積歷史。 |

因此：測試不是只比較預先整理的最終數字，確實執行修復程式；但固定正反例、人工故障注入與空白歷史容量測試，不能取代正式資料重播。

### Fixture 來源及初始狀態

來源為工作區五個 data/external_lexicon/openjlpt-vocab-n*.csv，未額外增補詞條；每個檔案 SHA256、欄位與空 reading 數在 final_acceptance_summary.json。

| Level | 原始 CSV 列數 | fixture 去重後 | verified_word | verified_verb | unknown | invalid |
|---|---:|---:|---:|---:|---:|---:|
| N5 | 662 | 662 | 454 | 105 | 102 | 1 |
| N4 | 632 | 630 | 375 | 156 | 96 | 3 |
| N3 | 1784 | 1709 | 1394 | 224 | 89 | 2 |
| N2 | 1793 | 1785 | 1253 | 373 | 154 | 5 |
| N1 | 3463 | 3303 | 2549 | 477 | 271 | 6 |

共 8334 列，按既有 normalized surface、依 N5 到 N1 的順序跨檔去重成 8089 筆。Fixture 將它們設為 general/openjlpt/active，保留來源 reading/level，POS 留空；不是正式 18636 筆 pool 的原樣複製，也不是正式 importer 的等價測試。

grammar fixture 是既有 100 筆 seed，每級 20、used_count=0。原 24 次測試的初始 materials/history 與 Bank 皆空；每天成功 save 後保留歷史到次日。N5-only 三日、N1-only 三日等舊測試使用同一 Harness。

SQLite 使用真實臨時 SQLite candidate/job/bank、隔離 CSV materials；PostgreSQL 使用真正本機 PostgreSQL 17.11 隨機 schema，把相同來源資料寫入實際表，以真正 SQL、transaction、materials table 執行。僅連線目標與模型呼叫被隔離替換；不是 Mock PostgreSQL。正式 PostgreSQL 18 的資料、權限、負載與執行計畫仍 NOT VERIFIED。

**測試品質邊界**：離線 Gemini 回應的例句與意思是固定值，動詞活用是簡化字串拼接，不是正確日語活用引擎。這能驗證格式、固定候選、流程與歷史，不證明真 Gemini 的詞義或活用品質。原 capacity 方法把容量結果另外記錄，unit PASS 本身不代表 30/30 容量 PASS；此次報告獨立判讀 successful_days。

## B. 同一批 N5 的舊／新實際差異

以下以相同 662 筆本地 N5 來源及實際基準分類器執行；不是正式 1950 筆盤點。舊／新完整分類 evidence 在 before.json、after.json。

| 詞條 | 舊結果 | 新結果 | 來源義項 |
|---|---|---|---|
| あびる | 未驗證動詞、Word 可誤收 | verified_verb | to bathe; to shower |
| かける | 未驗證動詞、Word 可誤收 | verified_verb | to call by phone |
| ある | verified_verb | unknown | to be; to have (inanimate) |
| かかる | verified_verb | unknown | to take time or money |

空白歷史：105 → 105，找回 2、轉 Unknown 2，**淨增 0**。帶入此次核對的 1169 個歷史 key：舊可用 0、新可用 2，新增可用的是 あびる、かける。ある、かかる 已在該歷史集合，不能再用來補正式 fresh 缺口。

這是實際執行舊／新分類器的同集合對照，不是舊版完整 web job 的重播。正式 pool 的讀音、表記、分類與新增七筆 eligible row 未取得，不能把此 0 → 2 當成正式 N5 庫存結論。

七個明確動詞正例的局部契約測試由舊 5/7 改善為新 7/7；六個明確非動詞仍不進 Verb，一個未有完整證據的片語轉 Unknown。沒有正式全池人工 gold，故全池誤收率／誤殺率 **NOT VERIFIED**。

## C. N5-only 第 22 日停止的原因

本次用真實 selector 再執行 30 次，沒有每日重置歷史：

- N5-only 正式 quota 是每天 **5 個 N5 動詞**；全等級則是 **每天每級 1 個動詞**。兩種配置不可互相替代驗收。
- 隔離初始已驗證 N5 動詞 105 個；前 21 日消耗 21×5=105，同時消耗 168 個 Word。
- 第 22 日前共使用 273 個 canonical key，662−273=**389 個未使用 N5 詞條**。
- 389 筆分成：verified_verb **0**、verified_word **286**、unknown **102**、rejected_invalid **1**。
- 286 是詞典支持的 Word 候選，不能稱其餘 103 筆全是確認非動詞；invalid 1 亦不應混入 Unknown 或有效供應量。
- Selector 再經格式、數量詞等安全過濾後，仍保留 **66 個 eligible unknown id**；它與原始分類器的 102 Unknown 是不同階段的計數。
- 回傳 classifier_uncertainty、candidate_exhausted=false，後 9 次模型呼叫均為 0，沒有保存失敗教材或污染歷史。

所以第 22 日停止是「本地已驗證可用動詞用完且仍有未解的分類」，**不是證明所有 N5 來源都是真正非動詞或永久耗盡**。同一第 22 日歷史套到舊分類器，尚有 ある、かかる 兩筆，但仍不足當日 quota 5。

逐日數字與第 22 日 rejection breakdown 在 release_diagnostics.json；未使用 N5 詞條逐筆分類亦保存在 remaining_n5。

## D. 正式 10/30 故障是否被重播並解除

**NOT VERIFIED，尚未完成同一正式候選池的重播。**

此次合法取得的正式唯讀證據：

- 僅 GET archive/materials/history，沒有呼叫 generate、gemini-step、model-check 或 smoke。
- 核對 151 份已保存教材，最新為 2026-10-28-1；擷取前後 archive 相同。
- 逐份教材詞條投影得到 **1169 個 canonical key**，等於最新教材內嵌的生成前歷史 1156 加本次保存詞條，亦與 10/30 log 的 151/1169 吻合。
- history/items 顯示層只得到 1162 個 key，遺漏表記為 あした、あわただしい、いい、いく、かばん、しゃべる、ずいぶん；不能直接拿顯示 API 代替排除集合。本次改用逐份教材投影並檢查集合相等，沒有修改正式 API。
- release_history_snapshot.json 保存請求 URL、時間、response hash、教材 lexical projection 與集合差異。這不是 READ ONLY transaction 的完整 DB snapshot，也不含正式 vocabulary_pool。

**新增混合來源容量驗收**：本地 CSV pool + 已核對的正式歷史集合，進入真正 selector 及隔離 save/reload。不是正式 pool replay。

| Backend / 初始歷史 | 全等級 7 日 | 全等級 30 日 | 停止時行為 |
|---|---:|---:|---|
| SQLite / 空白 | 7/7 | 30/30 | 此項來源容量成立，不代表正式剩餘量。 |
| PostgreSQL17 / 空白 | 7/7 | 30/30 | 同上。 |
| SQLite / 正式歷史 key 1169 | **2/7** | **2/30** | 第 3 日起 N5 classifier_uncertainty、模型呼叫 0。 |
| PostgreSQL17 / 正式歷史 key 1169 | **2/7** | **2/30** | 與 SQLite 一致，沒有跨級補位或歷史 reuse。 |

成功日均實際經 prepare → enrich(mock) → finalize → save → history read，8/5/5、fallback=0；失敗日不新增教材，歷史集合不變。正式 pool 的 7/30 日容量仍 NOT VERIFIED；上述混合測試容量則明確 **FAIL**，不能以 unit PASS 遮掩。

| Level | 混合測試初始有效未用 Verb | Unknown 未用 | 被歷史排除的來源列 | 其中 verified Verb | 第 2 日後有效未用 Verb |
|---|---:|---:|---:|---:|---:|
| N5 | 2 | 61 | 367 | 103 | 0 |
| N4 | 63 | 70 | 225 | 93 | 61 |
| N3 | 151 | 71 | 173 | 73 | 149 |
| N2 | 318 | 149 | 107 | 55 | 316 |
| N1 | 382 | 260 | 167 | 95 | 380 |

第 3 日及之後整個 job 在 Phase A 停止，所有級別當日消耗均 0；不能把其他級別餘量擅自填 N5。完整每天每級初始、消耗、餘量、Unknown、history excluded、quota/error 見 release_diagnostics.csv。此測試不把正式 grammar used_count 複製到隔離庫，因此不聲稱重現正式當日 grammar 排程。

## E. 仍需取得的最小正式唯讀資料

目前沒有正式 PostgreSQL SELECT-only DSN，也沒有同一時點的 vocabulary_pool 匯出。不得從本地 CSV 補出正式 missing row 或 244 筆。

最低需求是同一個 REPEATABLE READ、READ ONLY snapshot：

1. vocabulary_pool N5 全部 id，及 id、surface、base_form、normalized_key、reading、POS、level、source/category、status/active、quality/commonness 等實際 schema 內欄位；要包括被 SQL 排除的 1281 筆，不能只匯出成功候選。
2. 同 snapshot 已保存 materials 的 id、material_key/date/version、word/verb 原始 JSON 主詞條；此次 API 投影可作交叉核對，但不能替代 transaction consistency。
3. 10/30 failed Job 92b4532ee1844e7dbd3d47f6293ecbb5 的 selector cursor、候選 id、rejection breakdown、failed keys；只取詞條相關 cache，不讀 secret/settings。
4. 若要正式五級 7/30 日容量，還需 N4/N3/N2/N1 同樣來源與證據欄位，而非只有 COUNT。

```sql
BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;
SET LOCAL statement_timeout = '5s';
SET LOCAL lock_timeout = '1s';
SHOW transaction_read_only;
SELECT current_timestamp, version();
SELECT id FROM vocabulary_pool WHERE id > :last_id ORDER BY id LIMIT 50;
-- 每組 ids 一次 hydration；依實際欄位名增列必要 source/category/POS 等欄位。
SELECT id,
       convert_to(COALESCE(surface::text, ''), 'UTF8') AS surface,
       convert_to(COALESCE(jlpt_level::text, ''), 'UTF8') AS jlpt_level,
       convert_to(COALESCE(part_of_speech::text, ''), 'UTF8') AS part_of_speech
FROM vocabulary_pool WHERE id = ANY(:ids) ORDER BY id;
-- 同一 snapshot 取得必要 materials 與該 failed job 的詞條投影。
ROLLBACK;
```

:last_id/:ids 是參數示意，不是可直接貼入 psql 的語法。完整安全操作及互斥排除集重播計畫沿用 readonly_audit_plan.md。只允許 read-only role，不能執行 app 的 migration/ensure/backfill/tag 函式。

## F. 是否需要新的 N5 動詞來源、需要多少

**CONFIRMED（僅此隔離資料）**：現有來源加現有歷史只支持未來兩次全等級成功，不能支持 7/30 日。至少需要新增「有效且未在歷史」的 N5 動詞證據，可能來自現有 Unknown 的可信義項解歧，也可能需要另外核准的可信來源；不是直接再匯入相同 CSV。

- 混合測試全等級：未來 7 日需要 7−2=**至少 5 個**額外有效未用 N5 動詞；30 日需 30−2=**至少 28 個**。這只是正式配額下限，沒有包含拒絕率與 reserve 品質裕量。
- 空白歷史 N5-only：30 日需 150 個，現有 105，差 **45 個**有效候選；第 22 日要先補足當日 5 個。不能把 102 Unknown 直接加到供應量。
- 若從此次歷史狀態開始 N5-only，7/30 日分別需 35/150 個，與現有 2 相比是 33/148 個缺口；同樣只是本地來源 + 正式歷史的隔離下限。
- 正式 pool 補庫數量 **NOT VERIFIED**。正式公式為 max(0, horizon × 該級每日 quota − 正式已驗證未用量)，再加人工決定的 reserve 裕量。

先複核現有 Unknown 與 source/POS/義項，不改 level、不解除歷史、不擅自新增詞條。經驗證仍不足才提出人工核准的來源增補清單、license、level 出處、POS/義項與歷史比對；本次未匯入正式或隔離額外候選來製造容量 PASS。

## G. 是否仍有合法動詞未能使用

**CONFIRMED**：ある、かかる 在本地來源中有明確動詞 gloss，詞典亦有動詞義項；新版因同時有普通 Word 義項、來源缺 POS 而回 Unknown。這不是「確認非動詞」，但在目前無後續自動複核路徑的流程中仍無法成為正式候選。

當前 classify_lexeme 不讀 CSV English gloss，也沒有經核准的 source-sense index，因此這兩筆可解釋的動詞義項尚未自動解歧。不可直接放寬所有同形詞；建議另行核准帶來源義項的驗證索引或人工複核，並保留 noun/verb sense 與 history identity 的既有邊界。

其餘 Unknown 有多少真正合法動詞 **NOT VERIFIED**，不能用這兩例外推誤殺率。正式 244 筆中還有多少可找回亦 NOT VERIFIED。

## 分類 evidence 的實際保存範圍

本次另以實際 SQLite/隔離 PostgreSQL 完成 Phase A，確認各 13 筆 Word/Verb primary 的 evidence 寫入 Job JSON，load 後保留版本；清空程序 LRU 後，同 Job 的 DB cache 仍在；新 Job 不自動取得舊 Job 的計畫。結果見 release_evidence_checks.json。

| 問題 | 實際狀態 |
|---|---|
| 保存範圍 | candidate payload 的 classification_evidence、reading、POS 隨同 Job candidate plan 存 JSON；不是新的正式 evidence table。 |
| 跨 Job | 同 worker 可命中 process LRU；不保證跨 worker/restart 持久化，也沒有跨 Job evidence 查詢表。 |
| 版本 | classifier_version=unidic-lattice-contract-v2；source 為固定 MeCab/UniDic 版本。 |
| 失效 | 新呼叫 surface/reading/POS 改變會重算 fingerprint；selector context 含 classifier version/history digest。程序 LRU 的 key 本身不含版本，更新程式需重啟或清 cache；不是熱改常數即可失效。 |
| 指紋限制 | fingerprint 含 surface/reading/POS/version，不含 pool_id、來源檔 hash 或 sense_id；不能聲稱已建立完整 source-fingerprint 持久化契約。 |
| Unknown | job 保存 unknown id、retryable/verification_route；是可複核的標記，不是已實作的持久化複核佇列或 retry_after 機制。 |
| 同形異義 | 無 sense_id；詞典歧義可回 Unknown，歷史仍是 word+verb surface canonical union。未新加永久黑名單，但也不能聲稱已完整解決 sense-aware 去重。 |
| 與 materials 分離 | Phase A DB cache 寫入後 history key=0；失敗 job 不會標成 used。已保存教材才進 history。 |

本次未新增正式 Schema、分類證據表、cooldown 資料或持久化 rejection；仍需另外授權。

## 回歸重跑結果與容量判定

| 執行項目 | 實際結果 |
|---|---|
| 原 generation 套件 | 18 PASS，186.466 秒。 |
| 原 selector 套件，SQLite + 真實隔離 PG | 20 PASS、3 SKIP，56.363 秒。 |
| 新 lexical 套件，12 方法 × 2 backend | 24 PASS，598.443 秒。 |
| 本次 DB evidence 持久化查核 | SQLite/PG 各完成，Phase A 模型呼叫 0。 |
| 空白歷史全等級 7/30 日 | 兩 DB 皆 7/7、30/30。 |
| 空白歷史 N5-only 30 次 | 21 成功、9 classifier_uncertainty；容量 FAIL。 |
| 帶 1169 歷史 key 全等級 7/30 日 | 兩 DB 皆 2/7、2/30；容量 FAIL。 |
| 正式 PG 同候選池 replay / 真 Gemini | NOT VERIFIED / NOT TESTED。 |

合計既有及新增 **62 PASS、3 SKIP**，不是 65 PASS。三項 PG skip 為：非法 UTF-8 text 無法插入真 PG、driver decode fault 以 SQLite-backed 注入、CSV history 特例由 SQLite 執行；PG 另有真 materials table E2E，不能把這三項當 PG PASS。

新的 7/30 日逐日驗收嚴格以保存教材的主詞條建立 history，對每次成功實際 history reader 與預期集合相等作 assertion；沒有將 normalized_key/display aliases 的擴張 usage helper 當作正式 history reader。早期驗收工具的集合不一致曾使 assertion 停止，已修正工具後整組重跑；未改生產程式來配合測試。

## H. 發布建議

**NOT READY（本次完整正式故障解除與長期供應驗收）。**

可證明的改善是字典 lattice 分類、Word/Verb 契約、Unknown 錯誤區分與已有防護沒有退化。不能證明正式 N5 有足量，也不能把帶歷史的容量 FAIL 改名為 PASS。

發布前尚需：

1. 取得正式一致候選池快照，完成 1281/244/374 同集合稽核與 replay。
2. 解釋並處理有證據的動詞 Unknown；不能以解除歷史或增加黑名單處理。
3. 用正式有效剩餘來源、保留現有歷史，證明至少 7 日及所要求的 30 日供應，或明確承認容量不足並核准來源增補。
4. 需要真實 Gemini/Render smoke 時另外授權；本次不得執行。

### 本次新增檔案、重現方式及回退

- scripts/audit_phase3br_release.py：唯讀歷史蒐集、逐日隔離診斷、DB evidence 查核、fixture/test 摘要。
- reports/phase3br_2026_10_30/final_acceptance.md、final_acceptance_summary.json。
- final_acceptance_generation_tests.json、final_acceptance_selector_tests.json、final_acceptance_lexical_tests.json。
- release_history_snapshot.json、release_diagnostics.json、release_diagnostics.csv、release_evidence_checks.json。

沒有改寫原 review.md/summary.json/daily_inventory.csv/repair.patch，保留其先前證據與限制，應與本次報告一起閱讀。

離線重現測試需使用既有 phase3br-venv 與本機隔離 PostgreSQL；TEST_SELECTOR_POSTGRES_DSN 僅允許 localhost。test_lexical_contracts.py 需明確指定 LexicalContracts PostgreSQLLexicalContracts，避免匯入的別套測試被重複 discovery 計數。capture-history 為額外的正式 GET-only 模式，不是離線測試必要步驟。

本次沒有部署，無正式 rollback 可做。要撤回本次驗收只撤回上述新工具/報告，不可還原現有 Phase 3B-R 程式，更不可 reset 到舊 main。日後若回退修復，應另行審查以已部署 69207c2 為基準的反向差異，保留所有教材、Bank 與正式資料；程式 rollback 不會撤銷資料寫入。
