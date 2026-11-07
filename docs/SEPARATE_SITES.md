# প্রতি ভাষার জন্য আলাদা ওয়েবসাইট (Separate sites)

একই engine + একই brain; শুধু প্রতিটি ভাষা আলাদা Cloudflare Pages প্রজেক্ট/ডোমেইনে প্রকাশ হয়।

## সবচেয়ে সহজ: সম্পূর্ণ স্বয়ংক্রিয় (কিছুই করতে হবে না)
```yaml
site:
  auto_sites: "mvn"      # প্রতি ভাষা পায় https://mvn-<lang>.pages.dev, প্রজেক্ট প্রথম ডিপ্লয়ে নিজেই তৈরি হয়
```
শর্ত: নামটি (mvn-bn, mvn-en ...) Cloudflare-এ কারো না নেওয়া থাকতে হবে; নেওয়া থাকলে অন্য prefix দিন।
নিজস্ব ডোমেইন লাগলে নিচের হাতে-লেখা `sites:` ব্যবহার করুন (ডোমেইন কেনা/যুক্ত করা স্বয়ংক্রিয় নয়)।

## হাতে চালু করা (brain/config/newsbot.yaml → site: এর ভিতরে)
```yaml
site:
  sites:                       # থাকলে আলাদা-সাইট মোড; খালি/না থাকলে এক সাইটে সব ভাষা
    bn: {base_url: "https://bn.example.com/", pages_project: "mvn-bn"}
    en: {base_url: "https://example.com/",    pages_project: "mvn-en"}
    es: {base_url: "https://es.example.com/", pages_project: "mvn-es"}
    fr: {base_url: "https://fr.example.com/", pages_project: "mvn-fr"}
    hi: {base_url: "https://hi.example.com/", pages_project: "mvn-hi"}
    # analytics_token: ঐ সাইটের Cloudflare Web Analytics টোকেন (ঐচ্ছিক)
```
নিয়ম: `sites`-এর ভাষা = `languages` হুবহু; base_url ও project প্রতিটি আলাদা।

## Cloudflare-এ যা করতে হবে
1. ৫টি Pages প্রজেক্ট তৈরি করুন (Direct Upload), নাম উপরের `pages_project` এর সাথে মিলিয়ে।
2. API টোকেনে এই সব প্রজেক্টের Pages Edit অনুমতি থাকতে হবে।
3. প্রতিটিতে নিজস্ব ডোমেইন যুক্ত করুন (বা *.pages.dev ব্যবহার করুন)।

## যা স্বয়ংক্রিয়
- `scripts/build_sites.py` প্রতিটি ভাষার জন্য আলাদা বিল্ড (`site/public-<lang>`) ও ফাইল-সংখ্যা চেক করে।
- `scripts/deploy_sites.py` প্রতিটি সাইট ডিপ্লয় করে; একটি ফেল করলে বাকিগুলো চলে, workflow ফেল দেখায়।
- hreflang ও ভাষা-পরিবর্তন লিংক অন্য সাইটগুলোতে যায় (data/alts.json থেকে)। pages_project নাম সাইটে যায় না।

## ভিজিটর দেখা
- Cloudflare Web Analytics: Pages প্রজেক্ট → Metrics → Web Analytics চালু (কোনো কোড লাগে না), অথবা `analytics_token` দিন। কুকি লাগে না।
- Google Search Console: প্রতিটি সাইট আলাদা property হিসেবে যোগ করুন (sitemap স্বয়ংক্রিয়)।
- AdSense: সাইট-ভিত্তিক রিপোর্ট।
