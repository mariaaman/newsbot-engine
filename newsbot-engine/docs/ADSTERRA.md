# Adsterra (বা যেকোনো নেটওয়ার্ক যা স্ক্রিপ্ট/স্নিপেট দেয়) চালু করা

ইঞ্জিন এখন দুই ধরনের নেটওয়ার্ক জানে: `adsense` (ডিফল্ট) আর `adsterra`। `adsterra` মোডে আপনি ড্যাশবোর্ড থেকে পাওয়া কোড ব্রেইনের কনফিগে বসান।

## ধাপ
1. Adsterra-তে সাইট যোগ করুন, অনুমোদনের পর ফরম্যাট বেছে কোড কপি করুন।
2. ব্রেইনের `config/newsbot.yaml`-এ `ads:` অংশ এভাবে ভরুন (উদাহরণ; আপনার আসল কোড বসাবেন):

```yaml
ads:
  enabled: true
  network: adsterra
  consent_required: true      # EU/UK দর্শকের সম্মতির আগে কোনো বিজ্ঞাপন-স্ক্রিপ্ট লোড হয় না
  scripts:                    # সাইট-জোড়া স্ক্রিপ্ট (যেমন Social Bar); শুধু https
    - "https://pl000.example.com/abc/social.js"
  txt_lines:                  # ড্যাশবোর্ডের ads.txt লাইন, হুবহু
    - "adsterra.com, 1234567, DIRECT, <ড্যাশবোর্ডের আইডি>"
  slots:                      # প্রতি জায়গায় ড্যাশবোর্ডের HTML স্নিপেট (after_summary, mid_article, end_article)
    mid_article: |
      <script async data-cfasync="false" src="//pl000.example.com/abc/invoke.js"></script>
      <div id="container-abc"></div>
```
3. `deploy` চালান। `ads.txt` নিজে তৈরি হয় (`https://আপনার-সাইট/ads.txt` খুলে দেখুন)।
4. `readiness` চালান: `ads.scripts`/`ads.slots` ও `ads.txt_lines` যাচাই হয়।

## নিয়ম ও সুরক্ষা
- নীতিমালার পাতায় (about, privacy, terms...) ও ৪০৪ পাতায় কখনো বিজ্ঞাপন বসে না।
- `consent_required: true` হলে স্নিপেট `<template>`-এ অপেক্ষা করে, পাঠক "Accept" চাপলে তবেই চলে।
- স্ক্রিপ্ট শুধু `https`; `//host/x.js` আপনা-আপনি `https://host/x.js` হয়। `</template` বা HTML মন্তব্য থাকলে বিল্ড থামে (কোড ভেঙে বেরোনো আটকাতে)।
- নেটওয়ার্ক বদলাতে চাইলে `network: adsense` ও `publisher_id: pub-...` দিন; অন্য কিছু বদলাতে হয় না।
- খবরের সাইটে পপ-আন্ডার/অতি-বিরক্তিকর ফরম্যাট পাঠকের আস্থা কমায় এবং সম্পাদকীয় নীতির "বিজ্ঞাপন চিহ্নিত থাকে" কথার সঙ্গে মেলে না; Native/Banner দিয়ে শুরু করুন।
