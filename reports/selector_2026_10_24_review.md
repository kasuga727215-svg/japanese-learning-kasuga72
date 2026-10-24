# 2026-10-24 Selector 工程審查與回歸報告

## 基準與範圍

- 審查起點 HEAD：`1b9fafe98830de329fa62eeeba108137686f39c3`。
- 開始時沒有 tracked 程式修改；原有未追蹤檔案 `main` 未處理。
- 不假定此 HEAD 等於 Render 已部署版本。本次未取得 Render revision 或正式 PostgreSQL 連線。
- 未 Commit、Push、Merge、建立 PR 或部署。
- 沒有資料庫 migration、正式資料匯入、舊資料改寫或 Gemini 真實呼叫。

## 根因與前後比較

| 問題 | HEAD 實作 | 本次修改 |
| --- | --- | --- |
| 分類淘汰太晚 | ID 查詢未排除 business/advanced 等分類，hydration 後才拒絕 | 確定禁止的分類、來源、品質和狀態提前在 SQL 篩除，仍只 SELECT ID |
| 合格來源排在後面 | 按 ID 排序，有限掃描批數可能長期看不到 OpenJLPT | OpenJLPT 優先，其他允許來源其次；每個來源層保留穩定 ID cursor |
| 掃描中止等同 timeout | `selector_scan_incomplete` 被直接映射為 selector_timeout，job 失敗 | 單次最多 4 批、每批最多 800 IDs；這是 request 切片，不是整個來源上限；下一 micro-step 繼續 |
| Phase A 一次準備全部 pack | 每級各自時間預算，單一 request 可累加過久 | 每次 request 只處理一個 pack 的 bounded preparation slice，selector 共用最多 4 秒預算 |
| 進度遺失 | 候選計畫重建，cache 沒保留完整掃描旗標 | cache 保存部分已選候選、source tier、last ID、排除統計、壞 row 與完成旗標 |
| 字尾誤判 | `いす` 因為結尾 `す` 被判為 non_word | 缺 POS 時使用既有 UniDic 詞典證據；不以單一字尾認定動詞 |
| 歷史 key 膨脹 | 一筆詞條可能加入完整顯示文字、讀音、中文註解等多個 key | 只抽取已儲存 materials 的詞條身分，移除顯示註記；不讀失敗 job 或 bank 作永久排除 |

原有的 verb dictionary audit 在本次 HEAD 已經存在，不是這次才加入。
`だます` 透過既有經驗證的 `騙す/ダマス` 對應通過；`向こう` 等不會靠字尾通過。

## SQL 與安全 Hydration

ID 查詢形式仍為：

```sql
SELECT vp.id
FROM vocabulary_pool vp
WHERE vp.jlpt_level = :level
  AND vp.id > :last_id
  AND ...確定的來源、分類、狀態、品質條件...
ORDER BY vp.id ASC
LIMIT :slice_batch_size;
```

SQL 條件可以檢查來源與分類文字，但 SELECT 不回傳日文文字。
Hydration 每批 50 IDs，不是逐 ID 重查；只取候選需要的欄位，不讀完整 payload 或例句。
PostgreSQL 文字欄位仍經 `convert_to(..., 'UTF8')` 回傳 bytea，後端 safe_text 正規化。

可直接 SQL 排除的 category：

```text
advanced, archaic, brand, business, classical, company, finance, financial,
generated_compound, legal, medical, named_entity, person_name, place_name,
proper_noun, sensitive, specialized, technical, typo_or_noise, unknown
```

可直接排除的 source：`auto_generated`, `generated`, `seed_advanced_synthetic`, `synthetic`。
`seed_advanced` 不是單憑名稱全放行；其 business/advanced 分類仍被拒絕。
沒有把可疑組合詞改成 general 或替它們改 JLPT level。

壞資料處理：小批 hydration 發生 decode error 時遞迴二分，定位到單一 ID；
該 ID 記入 job 的 quarantine/skip 清單，其餘資料繼續使用。
SQLite 非法 byte 被 replacement character 揭示後也只跳過該 row。
這是 job 範圍的隔離，沒有永久刪除或更改 vocabulary_pool 資料。

來源真正掃描完畢而數量不足時，額外統計 `sql_category`、`sql_source`、`sql_status` 等排除數量。
未完成該掃描與診斷，不標記 candidate_exhausted。

## 時間預算與恢復

- PostgreSQL 每次查詢以剩餘 slice 時間設定真正的 statement_timeout。
- SQLite 使用 progress handler 中止超出預算的 SQL。
- 部分成功候選及最後處理 ID 在 slice 結束後保存，不重新抽詞。
- ID cursor 不會因換 request 而回到 0；切換來源層時才重置該層 cursor。
- 連續三個沒有掃描進展的逾時 slice 才停止為 selector_timeout，避免無限循環。
- 尚未輪到查詢、只是共用 request 預算用完的 level 不計入 stalled slices。
- Phase A 未全部完成時，回 `ok=true, continue_same_step=true`，Gemini call count 維持 0。
- refill 的搜尋切片也保留 cursor，不把尚未掃完當成 reserve 耗盡。
- 沒有調高 Gunicorn timeout，沒有新增 Gemini retry 或變更 chunk size。

錯誤分類：

| 分類 | 意義 |
| --- | --- |
| candidate_pool_exhausted | 必要 level 的合格來源已完整掃描，仍缺 primary；回傳 available、missing 與排除原因 |
| selector_scan_incomplete | 尚有來源待掃描；保存進度，後續 micro-step 繼續，不當成耗盡 |
| selector_timeout | 查詢切片持續無法前進；可控停止，不讓 worker 無限等待 |
| candidate_validation_failed | 候選資料或讀取無法恢復，不能假裝成詞庫耗盡 |

## 真正資料量與限制

**正式 Render 的 N1/N2 有效剩餘數量：未驗證。**
沒有正式 PostgreSQL 連線，因此不能從「總數 18,636」推導可用量，
也不能確認正式 `material_history_keys count=3237` 的組成。

本機只讀查詢的資料庫不同：`state.sqlite3` 共 10,389 筆，不含已匯入的 OpenJLPT。
本機 N1/N2 各 2,563 筆，全部為 seed_advanced 的 business=1,303、advanced=1,260，
依現有政策可用 fresh word/verb 都是 0。這不是 Render 的剩餘數量。
本機 N5 的 category 排除 1,281 可追溯到 business=651、advanced=630。
樣本包含 `業務管理上`、`業務分析化`、`業務評価性` 等組合詞，沒有將其直接放行。

另一份審查是讀取專案已存在的 OpenJLPT CSV，沒有正式匯入：

| 來源檔等級 | 檔案 rows | 字典與品質驗證後、排除本機材料歷史的 unique word | unique verb |
| --- | ---: | ---: | ---: |
| N2 | 1,793 | 1,303 | 374 |
| N1 | 3,463 | 2,701 | 504 |

上述是來源檔及本機歷史的可用量，不是正式環境的可用量，也沒有重新認證來源檔的 JLPT 分級。
本機 11 份 materials 的 canonical history key=110；舊擷取法=159。
多出的顯示 alias 樣本與完整各級統計見 `selector_sources_2026_10_24.json`。

## 已執行回歸

Gemini 輸出全部為 mock，外部 urllib 呼叫被禁止；source selection、job cache、bank、
parser、quota、reserve/replacement、finalize、save 和 reload 使用實際程式。
教材儲存不再 Mock：SQLite 模式走既有 materials CSV，PostgreSQL 走真實隔離 materials table。

### Selector 與資料庫

`scripts/test_selector_scan_regression.py`：23 個測試，20 通過、3 個明確 skip，0 failures/errors。

- SQLite、真正本機 PostgreSQL 17.11 都通過 N1/N2 前 2,400 筆禁止分類、後面才有 OpenJLPT 的案例。
- N2 回傳 3 筆，實際 hydration/驗證只需處理 3 筆；禁止資料已在 SQL 排除。
- 4,000 筆無效 POS 後才有合格詞條：第一次保存 ID=3200 與已找到的一筆，第二次到 ID=4003 選滿，沒有固定總掃描上限。
- N5 word 2 primary + 2 reserve，`いす` 通過、明確動詞不進 word。
- prepare 尚未完成或必要 verb 不足時，實際 Gemini mock call 數為 0。
- 真實 PostgreSQL 慢查詢被 statement_timeout 取消，保存進度後可以續跑。
- SQLite 壞 byte 與 driver decode fault injection 只隔離壞 row，不抹掉好資料。
- refill 跨切片繼續、同級 quota、不增加 Gemini call。
- SQLite 與 PostgreSQL 真正執行 prepare -> enrich -> finalize -> save -> reload；結果 8/5/5、fallback=0。
- 相同 job 再 finalize 不新增教材，不增加 Gemini call。

三個 skip 是 PostgreSQL 類別中重複的 SQLite 專屬案例：非法 SQLite TEXT、SQLite driver fault injection、CSV 專屬 history fixture。
這些在 SQLite 類別有實際執行；PostgreSQL UTF8 本身拒絕插入非法 UTF8 TEXT，不虛構同樣的資料。

本次一組實測 selector slice 時間，非 Render 延遲保證：

| 案例 | SQLite | 本機 PostgreSQL |
| --- | ---: | ---: |
| N2 排除 2,400 筆後選 3 | 4.30 ms | 14.14 ms |
| N1 排除 2,400 筆後選 3 | 已通過，完整 log 見 JSON | 14.41 ms |
| PostgreSQL 強制慢查詢 | 不適用 | 約 985 ms 受控取消，恢復後約 21 ms 選出 2 筆 |

### 生成契約與多日測試

`scripts/test_daily_fresh_generation.py` 完整執行 18 個測試，18 通過、0 failures/errors。
與 selector suite 合計 38 通過、3 個明確 skip；skip 不計入通過數。

`scripts/test_daily_fresh_generation.py` 使用 checked-in OpenJLPT 進入真正 selector，非預先塞候選。
完整案例涵蓋歷史去重、同級 reserve、source refill、耗盡前置檢查、不重掃已拒絕來源、
verb 正反例與だます、word 複合 POS、bank cache、N4 grammar json_truncated 同級補位、
save 失敗不污染 used 狀態、禁止跨級代替、無額外 recent 排除、同 job idempotency。
另加入 word Gemini 回錯 POS 後同級 reserve 補位。

| 設定 | 三日 | 30 日模擬 |
| --- | --- | --- |
| N5/N4/N3/N2/N1 | 3/3 成功 | 30/30 成功 |
| N5 only | 3/3 成功 | 21 日成功，後 9 次正確在 Phase A 回報 verb 耗盡，該次 Gemini call=0 |
| N1 only | 3/3 成功 | 30/30 成功 |

成功的每份教材均 word=8、verb=5、grammar=5、fallback=0，且各級配額精確。
N5-only 的耗盡不是「30 天全部生成成功」，測試通過代表不足處理正確，沒有拿非動詞、舊題庫或別級偽裝。
Grammar 維持 rotation，同日不重複，已有 bank 補全快取時不呼叫 Gemini。
舊的 `test_daily_fresh_contracts.py` 也已執行通過，但不以其預先候選 Mock 取代上述 source-to-selector 測試。

## 邊界檢查與尚未驗證

- 已對照 HEAD 的 AST：Gemini call/model config、compact_json/enrich prompt、save_material_for_date、api_materials、grammar selector 未修改。
- index.html、settings、Telegram、quiz、錯題、動詞練習、文法拆解程式與資料檔未修改。
- `python -m py_compile` 與 `git diff --check` 通過。
- 正式 Render PostgreSQL、實際 18,636 筆 inventory、正式歷史 3,237 keys、正式網路延遲及 Gunicorn 負載：未驗證。
- 真正 Gemini response、模型服務穩定性、實際 API 費用與正式生成成功率：未驗證，真實 API call=0。
- 多個不同 job 同時生成、生成期間並行匯入候選詞的競爭情境：未驗證。
- 本機 PostgreSQL 17.11 已停止，測試資料位於獨立臨時 schema/cluster，不接觸正式資料庫。

## 修改檔案與函式

執行時需一併審查三份檔案：

1. `app.py`：history canonical identities、word source POS、fetch/select candidate、source refill、reserve scan pending、cache/ready/error/preparation helpers；run_gemini_daily_fresh_pack 僅接續 pending scan，不變更 enrich chunk/config。
2. `services/candidate_selector.py`：新增 `id_query()`、`select_slice()`，SQL prefilter、keyset、小批 hydration、二分隔離、受控 deadline、統計。
3. `services/verb_candidate_audit.py`：新增 `verified_word_evidence()`；原 verb evidence/audit 規則未放寬。

測試與審查：

- `scripts/test_daily_fresh_generation.py`：真實 materials save/readback、word wrong-POS replacement。
- `scripts/test_selector_scan_regression.py`：新增 selector/SQLite/PostgreSQL/端到端測試。
- `scripts/audit_selector_sources.py`：新增唯讀資料源與 history 統計工具。
- 本報告與三份 JSON 結果。

## 重跑

在專案根目錄及已安裝 requirements 的 Python 環境執行：

```powershell
python scripts/test_daily_fresh_generation.py --report reports/daily_fresh_2026_10_24_regression.json
python scripts/test_selector_scan_regression.py --report reports/selector_scan_2026_10_24_tests.json
python scripts/audit_selector_sources.py --report reports/selector_sources_2026_10_24.json
```

PostgreSQL 測試需提供 `TEST_SELECTOR_POSTGRES_DSN`，且只接受 localhost/127.0.0.1 的隔離實例。
它建立隨機測試 schema，結束時只清除自己建立的 schema。未提供 DSN 時 PostgreSQL 測試會明確 skip，不會宣稱通過。
本次 Windows 測試使用臨時 Python 依賴目錄及 PostgreSQL 二進位；這些不屬於待推送檔案。

## Rollback

本次沒有 commit 或 migration。人工備份差異及新增檔後，將上述三個 tracked 執行/測試檔中實際存在於 HEAD 的
`app.py`、`services/verb_candidate_audit.py`、`scripts/test_daily_fresh_generation.py` 回復至 `1b9fafe`，即可回到原程式。
新增 `services/candidate_selector.py` 在回復 app.py 後不再被匯入，可留作審查資料。
新增測試與報告不影響執行。不要回滾或刪除 materials、bank、vocabulary_pool、grammar_pool 資料。
此處僅提供方式，未執行任何 restore/reset/revert。
