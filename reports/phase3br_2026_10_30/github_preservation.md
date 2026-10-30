# Phase 3B-R GitHub 保存與發布限制

日期：2026-10-30。分支：codex/phase3br-n5-classification-20261030。

**本分支只保存待審修復與證據；正式發布結論仍為 NOT READY。**

## 版本與隔離

Parent 基準固定為 69207c261d84a0fdb21f967a68cfa2e643713951，不從舊 main 重做或覆蓋。
使用獨立 Git worktree，原 main 工作區的 dirty / untracked 檔案全部保留。
七個既有修復／測試檔與原工作區逐一比對文字一致；沒有混入 Phase 3A、正式部署日誌或不相關的 main 檔案。

本次額外變更僅為稽核工具可攜性、安全掃描、Unknown 複核索引與報告：legacy classifier 直接從指定 Git commit 讀取，不再要求複製本機 tmp 目錄；沒有再次修改生產分類規則、Selector、模型設定、生成狀態機或儲存契約。

原 review.md / final_acceptance.md / summary.json / daily_inventory.csv / release_diagnostics.csv 原樣保存。
repair.patch 僅將空白上下文行的單一空格整理為空行，以通過 Git whitespace 檢查；hunk 內容與修復語意不變，已重新通過 reverse apply check。原始檔仍保留在原工作區。
舊報告的 committed=false 是當時狀態，不是本次 Push 狀態；實際保存 SHA 以 GitHub branch/commit 為準。

## 新增的逐筆 Unknown 複核索引

review_n5_unknowns.py 使用實際分類器重新核對 after.json 的 102 筆本地 N5 Unknown，輸出 n5_unknown_review.csv/json。
每筆附來源檔與行號、來源 SHA256、原 reading/gloss、N5 來源、classifier version、lemma、orthBase、POS evidence。

| 原因 | 數量 | 本次處理 |
|---|---:|---|
| ambiguous_word_verb_sense | 3 | 列出所有詞典義項，等待來源義項解歧。 |
| pos_or_segmentation_unresolved | 23 | 需要詞典／切分複核，不能推定為非動詞。 |
| reading_or_segmentation_unresolved | 76 | 需要讀音、表記及切分複核，不能全部放行。 |

**approved_count=0。** 這是可追溯複核佇列，不是 102 筆已完成人工／跨詞典確認，也不是正式 PostgreSQL 的 244 筆。

ある 的 UniDic 同時有 有る（動詞）與 或る（連體詞），來源 gloss 是 to be / to have。
かかる 同時有 掛かる／罹る（動詞）與 斯かる（連體詞），來源 gloss 是 to take time or money。
はく 同時有 履く／佩く／吐く／掃く（動詞）與 箔／白／拍（名詞），來源 gloss 是 to wear / to put on trousers。
三者缺少來源 POS，現有安全契約因此回 Unknown。假設有經驗證的動詞 POS 時可解歧，但 probe 不會把該假設寫入 pool 或變成已批准來源。

ある、かかる 已在既有歷史，不能再次作 fresh；はく 還需要獨立核對義項、可信表記關係及完整歷史，沒有計入已驗證可用量。

## 這次另外確認的歷史表記風險

前次已合法取得的唯讀歷史集合含 浴びる、掛ける，不含 あびる、かける。
所以現行 surface-canonical 規則下「多出兩個可用候選」，不等於證明多出兩個語彙／義項從未使用。
あびる／浴びる 有明顯的異表記重複風險；かける／掛ける 是多義詞，需對照來源與已保存義項才能判斷。

本次沒有改寫既有歷史，也沒有擅自加入 reading/lemma 全域合併；後者可能誤傷同音／同形異義。
未完成可信 canonical identity 複核前，2 日容量只能解讀為「現行表記 key 契約的隔離結果」，不能宣稱語義層級 strict fresh 已完整通過。

## 重新執行的供應測試

提交前在此 worktree 重跑：generation 18 PASS（204.656 秒）、selector 20 PASS/3 SKIP（58.107 秒）、lexical 24 PASS（705.471 秒）。
合計 62 PASS/3 SKIP；不是 65 PASS。原有的三項 PostgreSQL SKIP 理由原樣保留。
結果在 push_verification_generation_tests.json、push_verification_selector_tests.json、push_verification_lexical_tests.json。
git diff --check 與暫存後 diff --check 也必須通過才可提交；容量 FAIL 不會被測試 PASS 覆蓋。

push_supply_diagnostics.json/csv 是本次從此 worktree 執行的結果。
來源仍是既有 OpenJLPT，輸入前次核對的 1169 個正式歷史 key，沒有重置逐日歷史、沒有插入額外候選、沒有跨級補 quota。

| Backend | 全等級 7 日 | 全等級 30 日 |
|---|---:|---:|
| SQLite | 2/7 | 2/30 |
| 真正隔離 PostgreSQL 17 | 2/7 | 2/30 |

成功日皆 8/5/5、fallback=0，實際 save/reload；第 3 日起 Phase A classifier_uncertainty，模型呼叫為 0。
N5-only 空白歷史仍是 21/30 成功，後九次提前停止，不是 30 日全成功。
以上不是正式 pool replay，也不能替代正式 PostgreSQL 18 的剩餘庫存盤點。

## 正式資料與來源增補計畫

使用者確認目前沒有正式 SELECT-only 連線或完整快照，先保存修復。**正式容量 NOT VERIFIED。**
沒有建立正式角色、Schema、分類表或資料匯入，也沒有使用高權限正式連線作稽核。

下一步需要管理者提供：同一 read-only snapshot 的 N5 pool 逐筆 source/category/POS/reading/surface、已保存 materials lexical identities、失敗 job selector state。
安全查詢及隔離方式見 readonly_audit_plan.md；不得將密碼貼入聊天或 Git。

詞源增補應先做 evidence-only proposal，不直接匯入：

1. 將目前 Unknown 的原始 OpenJLPT 行號、gloss 與可信詞典 exact entry / reading / POS / sense restrictions 對齊。
2. 使用 EDRDG JMdict 的 entry/sense evidence 補充詞性證據；JMdict 本身不能作為 N5 分級依據。
3. N5 分級仍要有 OpenJLPT 或其 Waller JLPT 來源的逐筆依據，不能將 N4/N3 或字典常用動詞冒標 N5。
4. 對 proposed candidate 建立 source hash、entry id、sense id、dictionary version、reading restrictions、verified POS 及人工審核欄。
5. 將 materials 的可信同義表記命中另列供人工核准；Bank 仍只作補全快取，不作歷史排除。
6. 只有通過這些檢查、確實未使用的候選才可計入來源容量。新增候選清單與正式匯入須另外核准。

可靠來源參考：

- [EDRDG JMdict 專案與詞條格式](https://www.edrdg.org/wiki/JMdict-EDICT_Dictionary_Project.html)
- [EDRDG 授權](https://www.edrdg.org/edrdg/licence.html)
- 既有 data/external_lexicon/openjlpt-NOTICE.md：記錄來源與 N5–N1 分級出處，並明示並非官方發布的 JLPT 字表。

在現行表記 key 契約下，先前估計全等級 7/30 日至少差 5/28 個；若跨表記複核減少有效量，缺口會更大。正式數量未知，不能先承諾補 28 個就能通過。

## 敏感資料與授權

publication_credential_scan.json 記錄發布檔案的 SHA256、token/URL pattern 及 Python literal/default AST 掃描。
掃描不輸出疑似 secret 值；它是靜態檢查，不是對未知編碼／未知格式的絕對保證。

release_history_snapshot.json 是正式教材 lexical history 原始投影，**不在此提交**，保留原本機位置。
正式 Render 原始 LOG、Phase 3A 資料、不相關 main 檔案、.env、settings.json、DB 檔與憑證不在此提交。
其他公開來源 evidence JSON 及逐日計數報告已保留；開放來源的授權與 attribution 隨資料保存。

## 部署與回滾方案（僅計畫，未執行）

目前禁止部署；先完成正式候選快照、canonical identity、有效庫存及 7/30 日供應驗收。
達標後仍要另取得部署授權，記錄實際 Live SHA/Deploy ID、確認 DB backup/可恢復點、審查正式分支差異，再手動部署指定已測 SHA。
不自行 Merge main、開 PR、開啟 Auto-Deploy 或打正式 Gemini。

若之後獲准部署但服務退化，停止生成，保存 job/deploy/log，回滾到部署前實際成功的 Deploy ID；69207c2 只是目前已知基準，部署當日仍須重新確認。
程式 rollback 不會撤銷 DB 寫入，不能刪除教材、清 Bank 或重建正式資料庫。

本次 GitHub 保存不改 Render，因此本次不需要、也沒有執行正式 rollback。
