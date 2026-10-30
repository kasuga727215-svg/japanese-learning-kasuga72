# 正式資料唯讀稽核計畫（尚未執行）

## 權限與快照

目前沒有正式 PostgreSQL 唯讀角色連線；不能以 `state.sqlite3` 或 CSV 代替正式資料。不要把連線字串貼入報告或 Git。由管理者提供 SELECT-only 角色，或提供去識別化匯出。角色不可為 superuser、不可有 DML/DDL 權限。

在同一個 REPEATABLE READ、READ ONLY transaction 取得一致快照；設定 `statement_timeout=5s`、`lock_timeout=1s`。結束使用 ROLLBACK。不得 import/執行 app 的 ensure、migration、backfill 或 tagging 函式。

```sql
BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;
SET LOCAL statement_timeout = '5s';
SET LOCAL lock_timeout = '1s';
SHOW transaction_read_only;
SELECT current_timestamp, current_user, version();
SELECT jlpt_level, category, source, status, part_of_speech, COUNT(*)
FROM vocabulary_pool
GROUP BY jlpt_level, category, source, status, part_of_speech
ORDER BY jlpt_level, category, source, status, part_of_speech;
-- 接續下列安全分批讀取，最後 ROLLBACK。
```

## 必要逐筆資料

1. vocabulary_pool 全部五級的 `id, surface, base_form, normalized_key, reading_hiragana, part_of_speech, jlpt_level, category, source, source_name, source_license, status, quality, commonness_score, is_active, domain_tags`。
2. 真正已存 materials 的識別碼、版本及 `material_json`；不讀 Telegram secret、API key、帳號憑證、與詞條稽核無關的設定。
3. 10/30 失敗 job 的 candidate plan：僅保留已選／失敗 key、pool_id、分類器版本、selector state；不可把失敗 job 視為 used。

ID keyset 每批 50；文字欄位使用 `convert_to(COALESCE(column::text,''),'UTF8')` 讀成 bytes，離線 strict decode，失敗另記 id。不可使用 offset 大掃描或逐筆一次 SQL。SQL 排除集合須沿用版本 69207c2 的 CASE 優先序，完整記錄被排除 id，不能只取 COUNT。

```sql
SELECT id FROM vocabulary_pool WHERE id > :last_id ORDER BY id LIMIT 50;
-- 對上述 50 個 id，一次讀取指定欄位；不是 SELECT *。
SELECT id,
       convert_to(COALESCE(surface,''),'UTF8') AS surface,
       convert_to(COALESCE(part_of_speech,''),'UTF8') AS part_of_speech,
       convert_to(COALESCE(jlpt_level,''),'UTF8') AS jlpt_level
FROM vocabulary_pool WHERE id = ANY(:ids) ORDER BY id;
```

`:last_id/:ids` 是參數化查詢示意，不是直接可貼入 psql 的值。

## 1950 筆互斥分層

依既有 Selector 次序產生各 row 的 first rejection，另外保存其他可能命中的原因，不混入 first-rejection 加總：

1. SQL inactive/status/quality/commonness/category/source。
2. materials 或 current batch identity 命中；這兩者另外分開計數，輸出匹配的 material ID、item_type、原詞、canonical key。
3. separator/counter/known-non-verb。
4. 舊分類器結果與新四態結果，各附實際 UniDic nodes、orthBase、lemma、reading、POS、分類器版本。
5. 最後有效未使用數、unknown 數、bad encoding 數。

1281 筆須列出 source/category 交叉統計，對其中所有「詞典證據為 verb」的 row 全部複核。不可因其表記像動詞就解除 business/advanced 安全過濾。

244 筆須逐筆產生可重現的 evidence，不得使用本地 CSV 中自行湊出的 244 筆。分類至少包含：合法字典形、非動詞、表記差異、分詞限制、同音異義、缺 POS、證據不足。244 的這些標籤可以重疊；另加一個互斥的主原因欄才能相加。

374 筆命中須區分：同型同義、可信異表記、同音不同詞、同形異義、跨 item_type 碰撞、display annotation 清理、current batch。既有 production 契約是 word+verb 的 surface canonical union；本次不擅自改為 sense-aware 歷史排除。

## 分類證據持久化提案（需要另外核准）

建議另表 `candidate_classification_evidence`，不寫入 materials used 或改 vocabulary_pool category：

| 欄位 | 用途 |
|---|---|
| pool_id / item_type / canonical_identity | 來源候選及用途 |
| source_fingerprint / classifier_version | 來源或分類器改變時失效 |
| evidence_status | 四態結果 |
| reason / evidence_json | 可稽核依據，不是永久黑名單 |
| retry_after / created_at / updated_at | 暫時冷卻與重驗 |

唯一鍵應包含 pool_id、item_type、source_fingerprint、classifier_version。Unknown 不等於 rejected_invalid；只有相同版本與相同來源指紋、仍在冷卻期內才可暫緩。POS 正確的 bank cache 也不得變成 used history。

Migration 必須先在隔離 PostgreSQL 驗證 additive CREATE TABLE / index；舊程式忽略新表即可相容。Rollback 應先關閉 evidence 功能旗標、回退程式，保留 evidence 表供稽核，不刪除詞庫或教材。正式建表、匯入、回填及部署都另行授權，本階段一律未執行。
