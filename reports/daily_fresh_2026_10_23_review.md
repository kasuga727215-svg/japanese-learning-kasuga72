# 2026-10-22 Daily Fresh 工程審查與修復

審查日期：2026-10-23。修改基準：`6e1fae6`。

## 資料證據與限制

- 本機沒有 `DATABASE_URL`，未取得 Render 正式 PostgreSQL；正式 N5 剩餘動詞數、正式部署效能與正式生成結果均為**未驗證**。
- 本機 `state.sqlite3` 的 N5 為 1,341 筆，與 Render 日誌所述資料量不同，不能互相替代。
- 只讀核對本機詞性／UniDic 字典形、標準化 key 與全部 11 份本機教材後，N5 可驗證動詞 18 個，其中未進歷史教材且通過品質條件的有 **2 個：忘れる、終わる**。
- 這 2 個可用於本機全等級 N5 quota=1；不足以支持本機 N5-only verb quota=5。正式庫是否已耗盡仍需正式盤點。
- 本次沒有向正式資料庫新增、重標等級、匯入或刪除任何候選詞。

## 審查發現

1. **動詞 audit 以形狀放行。** 原碼允許「漢字＋動詞字尾」、任意 `verb_group` 與任意非空 `する/ずる` 語幹。`v` 的 substring POS 比對也會誤中 `adverb`，`動詞` substring 會誤中 `形容動詞`。
2. **category=1281 可以在本機完整重現。** `advanced/seed_advanced=630`、`business/seed_advanced=651`。全部 1,281 筆均無通過驗證的字典形動詞。抽樣：安全改善力、安全管理案、安全分析策、業務改善、業務管理上、業務分析化。生成來源是 `scripts/build_vocabulary_advanced_seed.py` 中的 domain/action/pattern 笛卡兒組合，等級是程式輪配，不能把這些列算成經驗證的 N5 動詞。保留原 category 排除，不藉此放寬詞源。
3. **`vocabulary_pool` 本機沒有 item_type 欄位。** `item_type=verb` 是 selector 的用途參數，不能證明資料詞性；category 是主題分類，也不能代替 POS。
4. **`rejected=0` 是誤導性統計。** 原本只統計已通過前置濾網之候選的第二次 audit；前置拒絕未納入。兩次相同的薄弱判斷不會識別誤放的非動詞。
5. **refill 重掃。** 每次從 offset 0 開始，缺少跨 request 的來源游標／耗盡記錄；三次條件相同的 refill 會重掃相同拒絕資料。
6. **跨級補備援會掩蓋缺口。** selector 把一般 JLPT 缺額搬往其他級，後面正式 verb quota 仍要求 N5。word contract 也曾以總數補足掩蓋逐級 primary 缺額。
7. **歷史規則不完整。** 原 `excluded_generated_or_recent` 名稱實際混合 materials keys 與當批 keys；daily fresh selector 沒有額外使用 recent blacklist 或 bank 排除，但歷史 helper 經由 recent reader 限制最多 600 筆。
8. **附註詞條漏排。** `あげる (=やる)` 原本未對上歷史中的 `あげる`。30 日跨 word/verb 歷史檢查重現後已修正。
9. **payload 等級可能錯。** Compact rows 不含 level；validator 原先只改 bank column，payload 可能仍保留 pack 第一級。新增逐級教材內容斷言後修正為來源 candidate 等級；舊快取轉教材時也以 bank column 為準。

## 修復內容

- 以專案既有 `mecab-python3==1.0.12`／`unidic-lite==1.0.8` 建立快取的詞典證據索引。未知 POS 不再憑字尾猜動詞；來源明確非動詞仍拒絕。詞典不可用時回 selector 失敗，不假稱詞庫耗盡。
- 單節點必須是詞典中的動詞字典形；する複合詞需有詞典的サ変語幹證據。`だます` 核對「騙す／ダマス」詞典條目，避免最佳斷詞與 `ます` 字尾誤判。此對照不新增候選，不指派 JLPT。
- 保留 `id_batch_safe_hydration`、每批 50 hydration、既有 SQL timeout。改用穩定 `id > last_id` 分頁，把游標、已拒絕統計、壞列隔離與耗盡狀態保存到 job cache。
- 同一 job 已完整掃完且條件未變，不再重掃。未完整掃完／有未恢復壞列，不能宣稱 candidate_exhausted。
- 一般 JLPT 缺額不跨級搬移；primary 逐級驗證，reserve 缺額獨立記錄。保留既有明確授權的 optional SNS 規則。
- word/verb 歷史排除只來自已保存 materials，包含全部歷史與既有附註別名比對；bank 維持補全快取，failed job 不算已學歷史。
- recoverable enrich 失敗會立即啟用同級 replacement/refill；來源 refill 最多 3 次。缺口未補滿時在 pack 階段停止，不等 finalize 掩蓋。
- 保留 grammar rotation、同級 reserve 與快取 reuse。N4 json_truncated 後會啟用 N4 備援。
- completed job 重送直接回已完成結果與相同 material_key，不再次生成、保存或增加 usage。

## 動詞判定對照

| 詞條 | 修改前 | 修改後 |
|---|---|---|
| 向こう／遠く／ずるずる／どうぞ宜しく／或る | 誤允許 | 拒絕 |
| だます | 誤拒絕 | 詞典證據允許 |
| コピーする／あいさつする／引っ掻く | 允許 | 保留，需詞典證據 |

以上對照實際以 `HEAD` 原函式及修正函式執行。正式庫 selector 無法作同資料集的前後比較；本機 N5 selector 要求 3 個時選到 2 個（忘れる、終わる），完整掃描約 71.72 ms，並如實記錄備援不足。

## 回歸測試範圍

`scripts/test_daily_fresh_generation.py` 使用既有 migration 建立全新暫存 SQLite，讀取版本庫內的 OpenJLPT CSV 與 grammar seed。執行真實 selector、job cache、micro-step、compact parser、upsert、rotation、finalize 與 usage 更新；僅 Gemini 回覆與教材落盤函式以 mock 隔離。禁止測試發出網路請求。測試不依賴本機未追蹤的 state.sqlite3。

| 要求 | 自動測試 |
|---|---|
| A/B/C | 全等級、N5-only、N1-only 各連續 3 日 |
| D | 三種設定各模擬 30 日，包括真正耗盡後持續驗證提前停止 |
| E/F | N5 primary 被拒絕後同級 reserve；無 reserve 時同級來源 refill |
| G/H | 只有 N5 verb 耗盡時 Phase A 的 Gemini call=0；已耗盡來源三次 refill 不重掃 |
| I/J | 正反例、だます、複合 POS、bank cache 與歷史 fresh 排除 |
| K | N4 grammar json_truncated 同級補位與 rotation |
| L | 保存失敗不增加 bank／grammar 使用次數、不新增歷史 |
| M/N | 每級 quota 與教材 payload 等級精確；超過 600 筆的舊教材仍排除，無 recent/blacklist 額外排除 |
| O | 完成 job 重送不增加 Gemini 呼叫、使用次數或教材筆數 |

最新實際執行結果與證據以同目錄 `daily_fresh_2026_10_23_tests.json` 為準。所有成功教材均檢查 word=8、verb=5、grammar=5，各級 quota 與 payload 一致，fallback_count 全為 0。

本機模擬來源結果：全等級 30/30 日、N1-only 30/30 日；N5-only 21 日成功，後 9 日在 enrich 前如實不足，額外 Gemini 呼叫為 0。這是有限詞源耗盡測試成功，並非宣稱 N5-only 能生成滿 30 日。

舊 `scripts/test_daily_fresh_contracts.py` 另行執行通過；它原有的三日名稱模擬不單獨當作端到端驗收。

## 未修改範圍與尚未解除的限制

- 以 AST 對照確認所有 `api_*` 路由、`save_material_for_date()`、settings resolver、Gemini call/config、compact prompt/parser、Telegram、quiz、動詞練習及文法拆解函式保持不變。沒有修改 UI、fallback 設定或 DB schema。
- POS allowlist 與 word 規則維持；只補附註詞條正規化、歷史一致性與逐級 quota 契約。grammar 的 rotation／來源／enrich 格式維持。
- Render 正式 N5 剩餘量、PostgreSQL UTF-8 路徑與正式 worker 耗時未驗證。沒有真 Gemini 呼叫；mock 無法證明模型對所有真實詞條的補全品質。
- 重送測試涵蓋依序重送已完成 job；沒有宣稱多 worker 同時 finalize 或「保存成功但寫回 job cache 前程序崩潰」具原子性。現有保存函式禁止修改，本次未加入跨系統交易。
- UniDic 不認得的詞會保守拒絕，需以可信詞性來源補充驗證；不能把原始 N5 詞彙總數當成有效動詞容量。

## 正式只讀盤點與補庫方案

在已設定 Render DATABASE_URL 的服務 Shell 執行：

```bash
python scripts/audit_daily_fresh_inventory.py
```

只讀查詢 N5，輸出可驗證動詞、materials 排除後餘量、category/source 統計與樣本。不呼叫 Gemini，不匯入詞庫，不輸出資料庫密碼。

若正式驗證為耗盡，先用現有 OpenJLPT 原始 N5 標記與可信日文詞典 POS 交叉核對，產生「新詞／既有詞／已學詞／未驗證詞／來源與授權」差異清單供確認；不可把 N4 詞改標 N5，也不可自行補造 N5。確認清單後才能另外授權匯入。本次未執行補庫。

## 推送與 Rollback

執行檔：`app.py`、`services/verb_candidate_audit.py`。

審查／測試檔：`scripts/audit_daily_fresh_inventory.py`、`scripts/test_daily_fresh_generation.py`、`scripts/test_daily_fresh_contracts.py`、本報告與測試 JSON。

安裝既有 requirements 後可執行：

```bash
python scripts/test_daily_fresh_contracts.py
python scripts/test_daily_fresh_generation.py --report reports/daily_fresh_2026_10_23_tests.json
```

回滾請在建立本次獨立 commit 後使用 `git revert <本次commit>`，或先將 Render 回滾至上一部署再 revert 該 commit。沒有 DB migration 需要撤回。不要加入既有未追蹤檔 `main`。本次未 commit、未 push、未部署。
