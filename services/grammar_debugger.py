import re


VERB_FORM_LABELS = {
    "renyou_form": "連用形",
    "te_form": "て形",
    "ta_form": "た形",
    "nai_form": "ない形",
    "ba_form": "ば形",
    "shieki_form": "使役形",
    "ukemi_form": "受身形",
}


def clean(value):
    text = str(value or "").strip()
    text = re.sub(r"[\s\u3000\u200b\u200c\u200d\ufeff]+", "", text)
    return text


def base_report(code, severity, payload):
    target = payload.get("target_text") or payload.get("prompt") or "未指定目標"
    expected = clean(payload.get("correct_answer"))
    actual = clean(payload.get("user_answer"))
    return {
        "error_code": code,
        "severity": severity,
        "target": target,
        "expected": expected,
        "actual": actual,
        "diagnosis": "此錯誤需人工確認。",
        "rule": "目前沒有足夠資訊套用更精準規則。",
        "quick_fix": ["請先確認題目要求的型態，再比對正確答案。"],
        "similar_examples": [],
        "hint_zh_tw": "此錯誤需人工確認。",
    }


def verb_group_hint(target, target_form):
    if target_form == "ba_form":
        return [
            "降る（ふる）→ 降れ（ふれ）＋ば → 降れば（ふれば）",
            "書く（かく）→ 書け（かけ）＋ば → 書けば（かけば）",
        ]
    if target_form == "renyou_form":
        return [
            "晴れる（はれる）→ 晴れ（はれ）",
            "食べる（たべる）→ 食べ（たべ）",
        ]
    if target_form == "nai_form":
        return [
            "褒める（ほめる）→ 褒めない（ほめない）",
            "食べる（たべる）→ 食べない（たべない）",
        ]
    return [
        "読む（よむ）→ 読んで（よんで）",
        "見る（みる）→ 見て（みて）",
    ]


def verb_report(payload):
    report = base_report("JP-VF-001", "error", payload)
    actual = report["actual"]
    expected = report["expected"]
    target_form = payload.get("target_form") or payload.get("question_type") or ""
    form_label = VERB_FORM_LABELS.get(target_form, target_form or "指定形")

    if target_form == "renyou_form" and ("させる" in actual or "せる" in actual):
        report.update(
            {
                "error_code": "JP-VF-002",
                "diagnosis": "你把連用形誤寫成使役形。連用形是ます形去掉ます，不表示讓某人做某事。",
                "rule": "連用形重點是接續ます或其他助動詞；使役形通常會出現させる或せる。",
                "quick_fix": ["晴れる（はれる）→ 晴れ（はれ）", "食べる（たべる）→ 食べ（たべ）"],
                "similar_examples": ["見る（みる）的連用形是見（み）。", "起きる（おきる）的連用形是起き（おき）。"],
                "hint_zh_tw": "看到させる時，通常要先懷疑自己是不是寫成使役形了。",
            }
        )
        return report

    if target_form in {"nai_form", "ukemi_form"} and "られ" in actual:
        report.update(
            {
                "error_code": "JP-VF-003",
                "diagnosis": "你的答案混入了られる系統，可能把ない形、受身形或可能形混在一起。",
                "rule": "上一段／下一段動詞的ない形通常是去掉る後加ない；受身或可能才常見られる。",
                "quick_fix": ["褒める（ほめる）→ 褒めない（ほめない）", "食べる（たべる）→ 食べない（たべない）"],
                "similar_examples": ["見る（みる）的ない形是見ない（みない）。", "起きる（おきる）的ない形是起きない（おきない）。"],
                "hint_zh_tw": "如果題目問ない形，先不要加入られる。",
            }
        )
        return report

    if target_form == "ba_form" and ("かれば" in actual or "られば" in actual):
        report.update(
            {
                "error_code": "JP-VF-001",
                "diagnosis": "ば形接續錯誤。五段動詞通常要把語尾改成え段後加ば，不是直接加れば。",
                "rule": "五段動詞ば形：う段語尾 → え段＋ば。",
                "quick_fix": verb_group_hint(report["target"], target_form),
                "similar_examples": ["吹く（ふく）→ 吹け（ふけ）＋ば → 吹けば（ふけば）", "読む（よむ）→ 読め（よめ）＋ば → 読めば（よめば）"],
                "hint_zh_tw": "請先找出動詞群組，再決定語尾如何變化。",
            }
        )
        return report

    report.update(
        {
            "diagnosis": f"{form_label}答案不一致，請確認動詞群組與接續規則。",
            "rule": "動詞變化需要先判斷五段、上下段或不規則，再套用指定形態。",
            "quick_fix": verb_group_hint(report["target"], target_form),
            "similar_examples": ["書く（かく）→ 書けば（かけば）", "食べる（たべる）→ 食べない（たべない）"],
            "hint_zh_tw": "先判斷動詞群組，再檢查語尾是否變成正確段。",
        }
    )
    return report


def particle_report(payload):
    report = base_report("JP-PT-001", "error", payload)
    report.update(
        {
            "diagnosis": "助詞選擇可能不符合句子的語法角色。",
            "rule": "が常標示主語或狀態對象，を常標示受詞，に常標示方向、時間或對象。",
            "quick_fix": ["先找動作主體。", "再找動作作用的對象。", "最後確認方向、時間或地點。"],
            "similar_examples": ["雨が降る（あめがふる）", "本を読む（ほんをよむ）"],
            "hint_zh_tw": "不要直接用中文的『在、把、對』硬套日文助詞。",
        }
    )
    return report


def translation_report(payload, severity="warning"):
    code = "JP-TR-001" if severity == "error" else "JP-SNS-001"
    report = base_report(code, severity, payload)
    extra = payload.get("extra") or {}
    trap = payload.get("error_category") or extra.get("literal_translation_trap") or "直翻可能造成語氣不自然。"
    rewrite = extra.get("natural_rewrite") or payload.get("correct_answer") or "請參考自然語境重新表達。"
    report.update(
        {
            "diagnosis": "翻譯語氣與自然日文語境不完全一致。" if severity == "warning" else "翻譯出現明顯直翻，可能偏離原句語感。",
            "rule": "SNS 語感應先判斷語氣，再翻成自然繁中，不要逐字搬運日文結構。",
            "quick_fix": [f"直翻陷阱：{trap}", f"自然改寫建議：{rewrite}"],
            "similar_examples": ["マジでしんどい → 真的讓人心好累", "尊すぎる → 太神了、太值得珍惜"],
            "hint_zh_tw": "先抓語氣，再決定中文要口語、吐槽、撒嬌或感動。",
        }
    )
    return report


def debug_grammar(payload):
    category = payload.get("error_category") or ""
    question_type = payload.get("question_type") or payload.get("target_form") or ""

    if category in {"口語語感不自然", "SNS語感錯"}:
        return translation_report(payload, "warning")
    if category in {"中文直翻造成不自然", "直翻不自然"}:
        return translation_report(payload, "error")
    if "助詞" in category:
        return particle_report(payload)
    if question_type in VERB_FORM_LABELS or "動詞" in category:
        return verb_report(payload)

    return base_report("JP-GEN-001", "info", payload)
