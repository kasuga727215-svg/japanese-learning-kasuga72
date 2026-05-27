import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "vocabulary_seed_advanced.json"


DOMAINS = [
    ("業務", "ぎょうむ", "業務"), ("市場", "しじょう", "市場"), ("顧客", "こきゃく", "顧客"),
    ("製品", "せいひん", "產品"), ("販売", "はんばい", "銷售"), ("契約", "けいやく", "契約"),
    ("品質", "ひんしつ", "品質"), ("人材", "じんざい", "人才"), ("組織", "そしき", "組織"),
    ("経営", "けいえい", "經營"), ("財務", "ざいむ", "財務"), ("会計", "かいけい", "會計"),
    ("物流", "ぶつりゅう", "物流"), ("在庫", "ざいこ", "庫存"), ("企画", "きかく", "企劃"),
    ("開発", "かいはつ", "開發"), ("設計", "せっけい", "設計"), ("運用", "うんよう", "運用"),
    ("保守", "ほしゅ", "維護"), ("教育", "きょういく", "教育"), ("研修", "けんしゅう", "培訓"),
    ("採用", "さいよう", "招募"), ("評価", "ひょうか", "評估"), ("成果", "せいか", "成果"),
    ("課題", "かだい", "課題"), ("予算", "よさん", "預算"), ("情報", "じょうほう", "資訊"),
    ("資料", "しりょう", "資料"), ("機能", "きのう", "功能"), ("仕様", "しよう", "規格"),
    ("戦略", "せんりゃく", "策略"), ("方針", "ほうしん", "方針"), ("施策", "しさく", "措施"),
    ("制度", "せいど", "制度"), ("環境", "かんきょう", "環境"), ("資源", "しげん", "資源"),
    ("技術", "ぎじゅつ", "技術"), ("研究", "けんきゅう", "研究"), ("分析", "ぶんせき", "分析"),
    ("需要", "じゅよう", "需求"), ("供給", "きょうきゅう", "供給"), ("価格", "かかく", "價格"),
    ("利益", "りえき", "利益"), ("費用", "ひよう", "費用"), ("効率", "こうりつ", "效率"),
    ("安全", "あんぜん", "安全"), ("危機", "きき", "危機"), ("リスク", "りすく", "風險"),
    ("地域", "ちいき", "地區"), ("社会", "しゃかい", "社會"), ("文化", "ぶんか", "文化"),
    ("言語", "げんご", "語言"), ("表現", "ひょうげん", "表達"), ("感情", "かんじょう", "情感"),
    ("記憶", "きおく", "記憶"), ("意識", "いしき", "意識"), ("習慣", "しゅうかん", "習慣"),
    ("関係", "かんけい", "關係"), ("状況", "じょうきょう", "狀況"), ("条件", "じょうけん", "條件"),
    ("目的", "もくてき", "目的"), ("手段", "しゅだん", "手段"), ("過程", "かてい", "過程"),
    ("結果", "けっか", "結果"), ("原因", "げんいん", "原因"), ("影響", "えいきょう", "影響"),
    ("傾向", "けいこう", "趨勢"), ("変化", "へんか", "變化"), ("成長", "せいちょう", "成長"),
    ("競争", "きょうそう", "競爭"), ("協力", "きょうりょく", "合作"), ("交渉", "こうしょう", "談判"),
    ("議論", "ぎろん", "討論"), ("判断", "はんだん", "判斷"), ("決定", "けってい", "決定"),
    ("共有", "きょうゆう", "共享"), ("進捗", "しんちょく", "進度"), ("対応", "たいおう", "應對"),
]

ACTIONS = [
    ("改善", "かいぜん", "改善"), ("管理", "かんり", "管理"), ("分析", "ぶんせき", "分析"),
    ("評価", "ひょうか", "評估"), ("検討", "けんとう", "檢討"), ("導入", "どうにゅう", "導入"),
    ("推進", "すいしん", "推動"), ("支援", "しえん", "支援"), ("調整", "ちょうせい", "調整"),
    ("確認", "かくにん", "確認"), ("報告", "ほうこく", "報告"), ("共有", "きょうゆう", "共享"),
    ("提案", "ていあん", "提案"), ("設計", "せっけい", "設計"), ("構築", "こうちく", "建構"),
    ("運用", "うんよう", "運用"), ("強化", "きょうか", "強化"), ("最適化", "さいてきか", "最佳化"),
    ("効率化", "こうりつか", "效率化"), ("可視化", "かしか", "可視化"), ("標準化", "ひょうじゅんか", "標準化"),
    ("自動化", "じどうか", "自動化"), ("高度化", "こうどか", "高度化"), ("安定化", "あんていか", "穩定化"),
    ("拡大", "かくだい", "擴大"), ("縮小", "しゅくしょう", "縮小"), ("削減", "さくげん", "削減"),
    ("向上", "こうじょう", "提升"), ("維持", "いじ", "維持"), ("把握", "はあく", "掌握"),
    ("解決", "かいけつ", "解決"), ("予測", "よそく", "預測"), ("比較", "ひかく", "比較"),
    ("整理", "せいり", "整理"), ("連携", "れんけい", "協作"), ("交渉", "こうしょう", "談判"),
    ("監視", "かんし", "監控"), ("検証", "けんしょう", "驗證"), ("修正", "しゅうせい", "修正"),
    ("更新", "こうしん", "更新"), ("拡張", "かくちょう", "擴充"), ("保護", "ほご", "保護"),
]

PATTERNS = [
    ("{d}{a}", "{dr}{ar}", "{dz}的{az}", "名詞"),
    ("{d}{a}策", "{dr}{ar}さく", "{dz}{az}策略", "名詞"),
    ("{d}{a}案", "{dr}{ar}あん", "{dz}{az}方案", "名詞"),
    ("{d}{a}力", "{dr}{ar}りょく", "{dz}{az}能力", "名詞"),
    ("{d}{a}率", "{dr}{ar}りつ", "{dz}{az}率", "名詞"),
    ("{d}{a}性", "{dr}{ar}せい", "{dz}{az}性", "名詞"),
    ("{d}{a}化", "{dr}{ar}か", "{dz}{az}化", "名詞"),
    ("{d}{a}上", "{dr}{ar}じょう", "{dz}{az}上", "名詞"),
]

SNS = [
    ("バズる", "ばずる", "在網路上爆紅、被大量轉發或討論", "動詞", "sns", "seed_sns"),
    ("エモい", "えもい", "很有氛圍、令人感動、很有情緒感染力", "い形容詞", "sns", "seed_sns"),
    ("めちゃくちゃ", "めちゃくちゃ", "非常、超級", "副詞", "sns", "seed_sns"),
    ("めっちゃ", "めっちゃ", "非常、超級", "副詞", "sns", "seed_sns"),
    ("てぇてぇ", "てぇてぇ", "尊い、太美好、太值得推了", "形容表現", "otaku_culture", "seed_sns"),
    ("限界オタク", "げんかいおたく", "情緒激動到極限的粉絲狀態", "名詞", "otaku_culture", "seed_sns"),
    ("沼る", "ぬまる", "沉迷到難以自拔", "動詞", "otaku_culture", "seed_sns"),
    ("刺さる", "ささる", "深深打中內心、很有共鳴", "動詞", "sns", "seed_sns"),
    ("尊い", "とうとい", "太神聖、太美好、太值得推", "い形容詞", "otaku_culture", "seed_sns"),
    ("反則", "はんそく", "太犯規、魅力強到不公平", "名詞", "sns", "seed_sns"),
]


def add(entries, seen, item):
    key = item["normalized_key"]
    if key in seen:
        return
    seen.add(key)
    entries.append(item)


def main():
    entries = []
    seen = set()
    levels = ["N5", "N4", "N3", "N2", "N1", "N2", "N1", "N3"]
    domain_order = []
    left = DOMAINS[:45]
    right = DOMAINS[45:]
    for i in range(max(len(left), len(right))):
        if i < len(left):
            domain_order.append((i, *left[i]))
        if i < len(right):
            domain_order.append((45 + i, *right[i]))

    for d_i, d, dr, dz in domain_order:
        for a_i, (a, ar, az) in enumerate(ACTIONS):
            for p_i, (surface_tpl, reading_tpl, meaning_tpl, pos) in enumerate(PATTERNS):
                surface = surface_tpl.format(d=d, a=a)
                reading = reading_tpl.format(dr=dr, ar=ar)
                meaning = meaning_tpl.format(dz=dz, az=az)
                level = levels[(d_i + a_i + p_i) % len(levels)]
                category = "business" if d_i < 45 else "advanced"
                add(entries, seen, {
                    "surface": surface,
                    "base_form": surface,
                    "normalized_key": surface,
                    "reading_hiragana": reading,
                    "meaning_zh": meaning,
                    "part_of_speech": pos,
                    "jlpt_level": level,
                    "category": category,
                    "example_sentence": f"{surface}を確認します。",
                    "example_translation_zh": f"確認{meaning}。",
                    "source": "seed_advanced",
                    "priority": 1,
                    "cooldown_days": 14,
                })
                if len(entries) >= 10250:
                    break
            if len(entries) >= 10250:
                break
        if len(entries) >= 10250:
            break

    for i, (surface, reading, meaning, pos, category, source) in enumerate(SNS):
        add(entries, seen, {
            "surface": surface,
            "base_form": surface,
            "normalized_key": surface,
            "reading_hiragana": reading,
            "meaning_zh": meaning,
            "part_of_speech": pos,
            "jlpt_level": "",
            "category": category,
            "example_sentence": f"この投稿は{surface}感じがします。",
            "example_translation_zh": f"這篇貼文有「{meaning}」的感覺。",
            "source": source,
            "priority": 2,
            "cooldown_days": 14,
        })

    OUT.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"path": str(OUT), "count": len(entries)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
