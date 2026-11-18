"""Output-language catalog: every language the SITE can publish in (the sources may be in any language).

Stdlib only (the site generator and the tests import it without pydantic). Adding a language needs only its code in
`site.languages`: every default below applies until a config value overrides it, so nothing else has to be edited.

Per language:
  name/native   English and own name (native is the language switcher label on the site)
  locale        Hugo languageCode (html lang, og:locale, RSS)
  rtl           right-to-left script (the page gets dir="rtl")
  spaceless     the script has no spaces between words (ja, zh, th): length is counted in CHARACTERS and `min_words`
                means characters for that language (a word count would be tiny and fail every article)
  names         the capitalised-Latin-name check of the QA gate is meaningful. Only for English (the fact sheet is English): in every other
                language the writer translates place and organisation names (Estados Unidos, États-Unis), which the check would reject
                again and again, and German capitalises every noun. The number check stays on for every language.
  min_words/title_len  QA length limits (qa.min_words / qa.title_len in newsbot.yaml override them)
  angle         default for write.local_angle        disclosure   default for site.ai_disclosure
  update_note   timeline line of an updated article
  ui            the Hugo UI labels (order = UI_KEYS) or None: the site then shows the English labels for that language
Every wording below is a first draft written without a native-speaker review: have the pages and labels of a language you
publish in read by someone who speaks it (docs/LANGUAGES.md)."""
from __future__ import annotations

import re
import unicodedata

UI_KEYS = ("skip", "home", "categories", "policies", "latest", "trending", "updated", "published", "summary", "timeline",
           "source_analysis", "confirmed", "unclear", "disputed", "sources", "tier", "corrections", "no_corrections",
           "corr_said", "corr_right", "no_stories", "newer", "older", "ad_label", "consent_text", "consent_accept",
           "consent_decline", "not_found", "not_found_text", "footer_note", "cat_bangladesh", "cat_world", "cat_politics",
           "cat_economy", "cat_technology", "cat_sports", "cat_science", "cat_entertainment", "cat_environment",
           "cat_education")

EN_UI = ("Skip to content", "Home", "Categories", "Site policies", "Latest", "Developing stories", "Updated", "Published",
         "Summary", "Timeline", "Source analysis", "Confirmed (2 or more independent sources)", "Unclear or single-source",
         "Disputed (sources disagree)", "Sources", "Tier", "Corrections", "No corrections so far. See our", "We said",
         "Correct:", "No stories yet.", "Newer", "Older", "Advertisement", "We use cookies for advertising. Do you accept?",
         "Accept", "Decline", "Page not found", "This page does not exist or has moved.",
         "Articles are AI-assisted and based on cited sources.", "Bangladesh", "World", "Politics", "Economy", "Technology",
         "Sports", "Science", "Entertainment", "Environment", "Education")

_UI = {
    "hi": ("सामग्री पर जाएँ", "मुखपृष्ठ", "श्रेणियाँ", "साइट नीतियाँ", "ताज़ा", "विकसित होती खबरें", "अपडेट किया गया", "प्रकाशित",
           "सारांश", "घटनाक्रम", "स्रोत विश्लेषण", "पुष्ट (2 या अधिक स्वतंत्र स्रोत)", "अस्पष्ट या एकल स्रोत",
           "विवादित (स्रोत असहमत हैं)", "स्रोत", "स्तर", "सुधार", "अब तक कोई सुधार नहीं। देखें हमारी", "हमने कहा था", "सही:",
           "अभी कोई खबर नहीं।", "नया", "पुराना", "विज्ञापन", "हम विज्ञापन के लिए कुकीज़ का उपयोग करते हैं। क्या आप सहमत हैं?",
           "स्वीकार करें", "अस्वीकार करें", "पृष्ठ नहीं मिला", "यह पृष्ठ मौजूद नहीं है या हटा दिया गया है।",
           "लेख एआई की सहायता से तैयार किए गए हैं और उद्धृत स्रोतों पर आधारित हैं।", "बांग्लादेश", "दुनिया", "राजनीति",
           "अर्थव्यवस्था", "प्रौद्योगिकी", "खेल", "विज्ञान", "मनोरंजन", "पर्यावरण", "शिक्षा"),
    "ur": ("مواد پر جائیں", "صفحۂ اول", "زمرے", "سائٹ کی پالیسیاں", "تازہ ترین", "جاری خبریں", "تازہ کاری", "شائع شدہ",
           "خلاصہ", "واقعات کی ترتیب", "ذرائع کا تجزیہ", "تصدیق شدہ (2 یا زیادہ آزاد ذرائع)", "غیر واضح یا ایک ذریعہ",
           "متنازع (ذرائع میں اختلاف)", "ذرائع", "درجہ", "اصلاحات", "اب تک کوئی اصلاح نہیں۔ ملاحظہ کریں ہماری", "ہم نے کہا تھا",
           "درست:", "ابھی کوئی خبر نہیں۔", "نیا", "پرانا", "اشتہار", "ہم اشتہارات کے لیے کوکیز استعمال کرتے ہیں۔ کیا آپ متفق ہیں؟",
           "قبول کریں", "مسترد کریں", "صفحہ نہیں ملا", "یہ صفحہ موجود نہیں یا منتقل ہو گیا ہے۔",
           "مضامین مصنوعی ذہانت کی مدد سے تیار کیے گئے ہیں اور حوالہ دیے گئے ذرائع پر مبنی ہیں۔", "بنگلہ دیش", "دنیا", "سیاست",
           "معیشت", "ٹیکنالوجی", "کھیل", "سائنس", "تفریح", "ماحولیات", "تعلیم"),
    "ar": ("انتقل إلى المحتوى", "الرئيسية", "الأقسام", "سياسات الموقع", "الأحدث", "أخبار متطورة", "حُدِّث", "نُشر", "ملخص",
           "التسلسل الزمني", "تحليل المصادر", "مؤكد (مصدران مستقلان أو أكثر)", "غير واضح أو مصدر واحد", "محل خلاف (المصادر تختلف)",
           "المصادر", "المستوى", "التصحيحات", "لا توجد تصحيحات حتى الآن. اطّلع على", "قلنا", "الصحيح:", "لا توجد أخبار بعد.",
           "الأحدث", "الأقدم", "إعلان", "نستخدم ملفات تعريف الارتباط للإعلانات. هل توافق؟", "موافق", "رفض", "الصفحة غير موجودة",
           "هذه الصفحة غير موجودة أو تم نقلها.", "المقالات بمساعدة الذكاء الاصطناعي وتستند إلى مصادر موثّقة.", "بنغلاديش", "العالم",
           "السياسة", "الاقتصاد", "التكنولوجيا", "الرياضة", "العلوم", "الترفيه", "البيئة", "التعليم"),
    "es": ("Saltar al contenido", "Inicio", "Categorías", "Políticas del sitio", "Últimas", "Noticias en desarrollo",
           "Actualizado", "Publicado", "Resumen", "Cronología", "Análisis de fuentes", "Confirmado (2 o más fuentes independientes)",
           "No claro o fuente única", "En disputa (las fuentes no coinciden)", "Fuentes", "Nivel", "Correcciones",
           "Sin correcciones por ahora. Consulte nuestra", "Dijimos", "Correcto:", "Aún no hay noticias.", "Más recientes",
           "Más antiguas", "Publicidad", "Usamos cookies para publicidad. ¿Acepta?", "Aceptar", "Rechazar", "Página no encontrada",
           "Esta página no existe o se ha movido.", "Los artículos se elaboran con ayuda de IA y se basan en fuentes citadas.",
           "Bangladés", "Mundo", "Política", "Economía", "Tecnología", "Deportes", "Ciencia", "Entretenimiento", "Medio ambiente",
           "Educación"),
    "fr": ("Aller au contenu", "Accueil", "Catégories", "Politiques du site", "Dernières", "Actualités en cours", "Mis à jour",
           "Publié", "Résumé", "Chronologie", "Analyse des sources", "Confirmé (2 sources indépendantes ou plus)",
           "Incertain ou source unique", "Contesté (les sources divergent)", "Sources", "Niveau", "Corrections",
           "Aucune correction pour l'instant. Voir notre", "Nous avions écrit", "Correct :", "Aucun article pour l'instant.",
           "Plus récents", "Plus anciens", "Publicité", "Nous utilisons des cookies publicitaires. Acceptez-vous ?", "Accepter",
           "Refuser", "Page introuvable", "Cette page n'existe pas ou a été déplacée.",
           "Les articles sont rédigés avec l'aide de l'IA et s'appuient sur des sources citées.", "Bangladesh", "Monde",
           "Politique", "Économie", "Technologie", "Sport", "Science", "Divertissement", "Environnement", "Éducation"),
    "de": ("Zum Inhalt springen", "Startseite", "Kategorien", "Richtlinien der Website", "Neueste", "Aktuelle Entwicklungen",
           "Aktualisiert", "Veröffentlicht", "Zusammenfassung", "Zeitverlauf", "Quellenanalyse",
           "Bestätigt (2 oder mehr unabhängige Quellen)", "Unklar oder nur eine Quelle", "Umstritten (Quellen widersprechen sich)",
           "Quellen", "Stufe", "Korrekturen", "Bisher keine Korrekturen. Siehe unsere", "Wir schrieben", "Richtig:",
           "Noch keine Meldungen.", "Neuer", "Älter", "Werbung", "Wir verwenden Cookies für Werbung. Stimmen Sie zu?", "Zustimmen",
           "Ablehnen", "Seite nicht gefunden", "Diese Seite existiert nicht oder wurde verschoben.",
           "Artikel entstehen mit KI-Unterstützung und beruhen auf genannten Quellen.", "Bangladesch", "Welt", "Politik",
           "Wirtschaft", "Technologie", "Sport", "Wissenschaft", "Unterhaltung", "Umwelt", "Bildung"),
    "pt": ("Ir para o conteúdo", "Início", "Categorias", "Políticas do site", "Mais recentes", "Notícias em desenvolvimento",
           "Atualizado", "Publicado", "Resumo", "Cronologia", "Análise das fontes", "Confirmado (2 ou mais fontes independentes)",
           "Incerto ou fonte única", "Contestado (as fontes divergem)", "Fontes", "Nível", "Correções",
           "Nenhuma correção até agora. Veja nossa", "Dissemos", "Correto:", "Ainda não há notícias.", "Mais novas", "Mais antigas",
           "Publicidade", "Usamos cookies para publicidade. Você aceita?", "Aceitar", "Recusar", "Página não encontrada",
           "Esta página não existe ou foi movida.", "Os artigos são feitos com auxílio de IA e baseiam-se em fontes citadas.",
           "Bangladesh", "Mundo", "Política", "Economia", "Tecnologia", "Esportes", "Ciência", "Entretenimento", "Meio ambiente",
           "Educação"),
    "id": ("Langsung ke konten", "Beranda", "Kategori", "Kebijakan situs", "Terbaru", "Berita yang berkembang", "Diperbarui",
           "Diterbitkan", "Ringkasan", "Kronologi", "Analisis sumber", "Terkonfirmasi (2 sumber independen atau lebih)",
           "Belum jelas atau satu sumber", "Diperselisihkan (sumber berbeda)", "Sumber", "Tingkat", "Koreksi",
           "Belum ada koreksi. Lihat", "Kami menulis", "Yang benar:", "Belum ada berita.", "Lebih baru", "Lebih lama", "Iklan",
           "Kami menggunakan cookie untuk iklan. Apakah Anda setuju?", "Setuju", "Tolak", "Halaman tidak ditemukan",
           "Halaman ini tidak ada atau sudah dipindahkan.", "Artikel dibuat dengan bantuan AI dan berdasarkan sumber yang dikutip.",
           "Bangladesh", "Dunia", "Politik", "Ekonomi", "Teknologi", "Olahraga", "Sains", "Hiburan", "Lingkungan", "Pendidikan"),
    "ru": ("Перейти к содержимому", "Главная", "Рубрики", "Правила сайта", "Последние", "Развивающиеся события", "Обновлено",
           "Опубликовано", "Кратко", "Хронология", "Анализ источников", "Подтверждено (2 и более независимых источника)",
           "Неясно или один источник", "Оспаривается (источники расходятся)", "Источники", "Уровень", "Исправления",
           "Исправлений пока нет. См. наши", "Мы писали", "Верно:", "Новостей пока нет.", "Новее", "Старее", "Реклама",
           "Мы используем файлы cookie для рекламы. Вы согласны?", "Принять", "Отклонить", "Страница не найдена",
           "Этой страницы нет или она перемещена.", "Статьи подготовлены с помощью ИИ и основаны на указанных источниках.",
           "Бангладеш", "Мир", "Политика", "Экономика", "Технологии", "Спорт", "Наука", "Развлечения", "Окружающая среда",
           "Образование"),
    "tr": ("İçeriğe geç", "Ana sayfa", "Kategoriler", "Site politikaları", "En yeni", "Gelişen haberler", "Güncellendi",
           "Yayımlandı", "Özet", "Zaman çizelgesi", "Kaynak analizi", "Doğrulandı (2 veya daha fazla bağımsız kaynak)",
           "Belirsiz veya tek kaynak", "Tartışmalı (kaynaklar çelişiyor)", "Kaynaklar", "Düzey", "Düzeltmeler",
           "Şimdiye kadar düzeltme yok. Bkz.", "Şunu yazmıştık", "Doğrusu:", "Henüz haber yok.", "Daha yeni", "Daha eski",
           "Reklam", "Reklam için çerez kullanıyoruz. Kabul ediyor musunuz?", "Kabul et", "Reddet", "Sayfa bulunamadı",
           "Bu sayfa yok ya da taşındı.", "Makaleler yapay zekâ desteğiyle hazırlanır ve alıntılanan kaynaklara dayanır.",
           "Bangladeş", "Dünya", "Siyaset", "Ekonomi", "Teknoloji", "Spor", "Bilim", "Eğlence", "Çevre", "Eğitim"),
    "ja": ("本文へ移動", "ホーム", "カテゴリ", "サイトポリシー", "最新", "進行中のニュース", "更新", "公開", "要約", "時系列",
           "情報源の分析", "確認済み(独立した2つ以上の情報源)", "不明または単一の情報源", "見解が分かれている(情報源が一致しない)",
           "情報源", "区分", "訂正", "これまでに訂正はありません。次をご覧ください:", "当初の記述", "正しくは:",
           "記事はまだありません。", "新しい記事", "古い記事", "広告", "広告のためにCookieを使用します。同意しますか?", "同意する",
           "同意しない", "ページが見つかりません", "このページは存在しないか、移動されました。",
           "記事はAIの支援で作成され、引用した情報源に基づいています。", "バングラデシュ", "国際", "政治", "経済", "テクノロジー",
           "スポーツ", "科学", "エンタメ", "環境", "教育"),
    "zh": ("跳到内容", "首页", "分类", "网站政策", "最新", "持续报道", "更新于", "发布于", "摘要", "时间线", "信源分析",
           "已证实(2个或以上独立信源)", "不明确或单一信源", "存在争议(信源说法不一)", "信源", "等级", "更正",
           "目前没有更正。请查看我们的", "我们曾报道", "正确内容:", "暂无报道。", "较新", "较早", "广告",
           "我们使用Cookie投放广告。您是否同意?", "同意", "拒绝", "页面未找到", "该页面不存在或已移动。",
           "文章由AI辅助生成,并基于所引用的信源。", "孟加拉国", "国际", "政治", "经济", "科技", "体育", "科学", "娱乐", "环境",
           "教育"),
    "ko": ("본문으로 건너뛰기", "홈", "카테고리", "사이트 정책", "최신", "진행 중인 소식", "업데이트", "게시", "요약", "타임라인",
           "출처 분석", "확인됨(독립 출처 2개 이상)", "불분명 또는 단일 출처", "논란 중(출처 간 불일치)", "출처", "등급", "정정",
           "지금까지 정정 사항이 없습니다. 다음을 참고하세요:", "당시 보도", "바른 내용:", "아직 기사가 없습니다.", "최신", "이전",
           "광고", "광고를 위해 쿠키를 사용합니다. 동의하십니까?", "동의", "거부", "페이지를 찾을 수 없습니다",
           "이 페이지는 없거나 이동되었습니다.", "기사는 AI의 도움으로 작성되며 인용된 출처에 기반합니다.", "방글라데시", "세계",
           "정치", "경제", "기술", "스포츠", "과학", "엔터테인먼트", "환경", "교육"),
}

_DISCLOSURE = {
    "bn": "এই নিবন্ধটি এআই-সহায়তায় তৈরি এবং আমাদের সম্পাদকীয় নীতি মেনে চলে।",
    "en": "This article is AI-assisted and follows our editorial policy.",
    "hi": "यह लेख एआई की सहायता से तैयार किया गया है और हमारी संपादकीय नीति का पालन करता है।",
    "ur": "یہ مضمون مصنوعی ذہانت کی مدد سے تیار کیا گیا ہے اور ہماری ادارتی پالیسی کی پابندی کرتا ہے۔",
    "ar": "أُعدّت هذه المقالة بمساعدة الذكاء الاصطناعي وفق سياستنا التحريرية.",
    "ne": "यो लेख एआईको सहयोगमा तयार पारिएको हो र हाम्रो सम्पादकीय नीति पालना गर्छ।",
    "id": "Artikel ini dibuat dengan bantuan AI dan mengikuti kebijakan editorial kami.",
    "es": "Este artículo se ha elaborado con ayuda de IA y sigue nuestra política editorial.",
    "fr": "Cet article a été rédigé avec l'aide de l'IA et respecte notre politique éditoriale.",
    "de": "Dieser Artikel entstand mit KI-Unterstützung und folgt unseren redaktionellen Richtlinien.",
    "pt": "Este artigo foi elaborado com auxílio de IA e segue a nossa política editorial.",
    "ru": "Эта статья подготовлена с помощью ИИ и соответствует нашей редакционной политике.",
    "tr": "Bu makale yapay zekâ desteğiyle hazırlanmıştır ve yayın politikamıza uygundur.",
    "ja": "この記事はAIの支援で作成され、当サイトの編集方針に従っています。",
    "zh": "本文由AI辅助生成,并遵循我们的编辑政策。",
    "ko": "이 기사는 AI의 도움으로 작성되었으며 편집 정책을 따릅니다.",
    "fa": "این مقاله با کمک هوش مصنوعی تهیه شده و از سیاست تحریریهٔ ما پیروی می‌کند.",
    "vi": "Bài viết này được tạo với sự hỗ trợ của AI và tuân theo chính sách biên tập của chúng tôi.",
    "th": "บทความนี้จัดทำด้วยความช่วยเหลือของ AI และเป็นไปตามนโยบายกองบรรณาธิการของเรา",
    "sw": "Makala hii imeandaliwa kwa msaada wa AI na inafuata sera yetu ya uhariri.",
}
_NOTE = {
    "bn": "নতুন যাচাই করা তথ্য যোগ করে প্রতিবেদন হালনাগাদ করা হয়েছে।",
    "en": "Article updated with new verified facts.",
    "hi": "नए सत्यापित तथ्य जोड़कर लेख अपडेट किया गया।",
    "ur": "نئے تصدیق شدہ حقائق شامل کر کے مضمون کو اپ ڈیٹ کیا گیا۔",
    "ar": "حُدِّثت المقالة بإضافة وقائع جديدة تم التحقق منها.",
    "ne": "नयाँ प्रमाणित तथ्य थपेर लेख अद्यावधिक गरियो।",
    "id": "Artikel diperbarui dengan fakta baru yang telah diverifikasi.",
    "es": "Artículo actualizado con nuevos datos verificados.",
    "fr": "Article mis à jour avec de nouveaux faits vérifiés.",
    "de": "Artikel mit neuen, geprüften Fakten aktualisiert.",
    "pt": "Artigo atualizado com novos fatos verificados.",
    "ru": "Статья обновлена: добавлены новые проверенные факты.",
    "tr": "Makale, yeni doğrulanmış bilgilerle güncellendi.",
    "ja": "新たに確認された事実を加えて記事を更新しました。",
    "zh": "已加入新核实的事实并更新文章。",
    "ko": "새로 확인된 사실을 반영해 기사를 업데이트했습니다.",
    "fa": "مقاله با افزودن واقعیت‌های تازه‌تأییدشده به‌روزرسانی شد.",
    "vi": "Bài viết đã được cập nhật với các thông tin mới được xác minh.",
    "th": "อัปเดตบทความด้วยข้อเท็จจริงใหม่ที่ผ่านการตรวจสอบแล้ว",
    "sw": "Makala imesasishwa kwa ukweli mpya uliothibitishwa.",
}

# code: (English name, native name, locale, rtl, spaceless, names check, min_words(or chars), title_len)
_BASE = {
    "bn": ("Bengali", "বাংলা", "bn-BD", False, False, True, 450, (15, 110)),
    "en": ("English", "English", "en", False, False, True, 500, (20, 100)),
    "hi": ("Hindi", "हिन्दी", "hi-IN", False, False, False, 450, (15, 110)),
    "ur": ("Urdu", "اردو", "ur-PK", True, False, False, 450, (15, 110)),
    "ar": ("Arabic", "العربية", "ar", True, False, False, 400, (15, 110)),
    "ne": ("Nepali", "नेपाली", "ne-NP", False, False, False, 400, (15, 110)),
    "id": ("Indonesian", "Bahasa Indonesia", "id-ID", False, False, False, 450, (20, 110)),
    "es": ("Spanish", "Español", "es", False, False, False, 450, (20, 110)),
    "fr": ("French", "Français", "fr", False, False, False, 450, (20, 110)),
    "de": ("German", "Deutsch", "de", False, False, False, 400, (20, 110)),
    "pt": ("Portuguese", "Português", "pt", False, False, False, 450, (20, 110)),
    "ru": ("Russian", "Русский", "ru", False, False, False, 400, (20, 110)),
    "tr": ("Turkish", "Türkçe", "tr", False, False, False, 400, (20, 110)),
    "ja": ("Japanese", "日本語", "ja", False, True, False, 900, (10, 60)),
    "zh": ("Chinese", "中文", "zh-CN", False, True, False, 800, (10, 60)),
    "ko": ("Korean", "한국어", "ko", False, False, False, 350, (12, 70)),
    "fa": ("Persian", "فارسی", "fa-IR", True, False, False, 400, (15, 110)),
    "vi": ("Vietnamese", "Tiếng Việt", "vi-VN", False, False, False, 550, (20, 110)),
    "th": ("Thai", "ไทย", "th-TH", False, True, False, 1400, (15, 110)),
    "sw": ("Swahili", "Kiswahili", "sw", False, False, False, 400, (20, 110)),
}
_ANGLE = {
    "bn": "Explain what this means for readers in Bangladesh.",
    "en": "Give short background and an international view.",
}


def _build() -> dict[str, dict]:
    out = {}
    for code, (name, native, locale, rtl, spaceless, names, mw, tl) in _BASE.items():
        out[code] = {
            "code": code, "name": name, "native": native, "locale": locale, "rtl": rtl, "spaceless": spaceless,
            "names": names, "min_words": mw, "title_len": tl,
            "angle": _ANGLE.get(code, f"Explain what this means for readers who speak {name} and give short background."),
            "disclosure": _DISCLOSURE[code], "update_note": _NOTE[code],
            "ui": dict(zip(UI_KEYS, _UI[code])) if code in _UI else None,
        }
    out["en"]["ui"] = dict(zip(UI_KEYS, EN_UI))
    return out


LANGUAGES: dict[str, dict] = _build()
LANG_NAMES: dict[str, str] = {c: m["name"] for c, m in LANGUAGES.items()}
CODE_RE = re.compile(r"^[a-z]{2,3}$")


def is_supported(code: str) -> bool:
    return code in LANGUAGES


def meta(code: str) -> dict:
    """The catalog entry, or a neutral one for a code that is not in the catalog (never a KeyError mid-run)."""
    return LANGUAGES.get(code) or {"code": code, "name": code, "native": code, "locale": code, "rtl": False, "spaceless": False,
                                   "names": True, "min_words": 450, "title_len": (15, 110),
                                   "angle": "Give short background for readers who speak this language.",
                                   "disclosure": _DISCLOSURE["en"], "update_note": _NOTE["en"], "ui": None}


def _get(d, lang, default):
    try:
        v = d.get(lang)
    except AttributeError:
        v = None
    return default if v is None else v


def min_words(q, lang: str) -> int:
    """Minimum length: qa.min_words[lang] when set, else the catalog default (characters for spaceless scripts)."""
    return int(_get(getattr(q, "min_words", {}) or {}, lang, meta(lang)["min_words"]))


def title_len(q, lang: str) -> tuple[int, int]:
    lo, hi = _get(getattr(q, "title_len", {}) or {}, lang, meta(lang)["title_len"])
    return int(lo), int(hi)


def local_angle(write_cfg, lang: str) -> str:
    return str(_get(getattr(write_cfg, "local_angle", {}) or {}, lang, meta(lang)["angle"]))


def disclosure(site_cfg, lang: str) -> str:
    return str(_get(getattr(site_cfg, "ai_disclosure", {}) or {}, lang, meta(lang)["disclosure"]))


def update_note(lang: str) -> str:
    return meta(lang)["update_note"]


def length_units(text: str, lang: str, word_list: list[str]) -> int:
    """Article length for the min_words gate: words, or letters/digits for a script without spaces (ja, zh, th)."""
    if meta(lang)["spaceless"]:
        return sum(1 for c in text if unicodedata.category(c)[0] in "LN")
    return len(word_list)


def unit_name(lang: str) -> str:
    return "characters" if meta(lang)["spaceless"] else "words"


def ui_strings(lang: str) -> dict[str, str]:
    """All UI labels of a language; a label (or the whole language) without a translation shows English."""
    base = dict(zip(UI_KEYS, EN_UI))
    base.update(meta(lang).get("ui") or {})
    return base
