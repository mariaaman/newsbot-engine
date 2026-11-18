# ভিজিটর দেখা (Cloudflare Web Analytics, ফ্রি, কুকি ছাড়া)

## Cloudflare API টোকেনে যে অনুমতি লাগবে (একই টোকেন, শুধু অনুমতি যোগ)
- Account → **Workers Scripts: Edit**, **Workers KV Storage: Edit**   (dashboard Worker, আগে থেকেই)
- Account → **Cloudflare Pages: Edit**                                  (সাইট ডিপ্লয় ও প্রজেক্ট তৈরি)
- Account → **Account Settings: Edit**                                  (প্রতি সাইটে ভিজিটর কাউন্টার স্বয়ংক্রিয় চালু)
- Account → **Account Analytics: Read**                                 (dashboard-এ ভিজিটর সংখ্যা পড়া)
আলাদা read-only টোকেন চাইলে (Account Analytics: Read + Account Settings: Read) সেটা GitHub secret `CLOUDFLARE_ANALYTICS_TOKEN` নামে রাখুন; dashboard তখন ওটাই ব্যবহার করবে।

## চালু করার ক্রম
1. টোকেনে অনুমতি যোগ করুন।
2. deploy workflow চালান (প্রতি সাইটে কাউন্টার চালু হয়, পেজে স্নিপেট বসে)।
3. **setup-dashboard** workflow আবার চালান (Worker secret নেয়)। এতে সবাই লগআউট হয় ও ২-ধাপ রিসেট হয়, তাই আবার লগইন করুন।
4. dashboard → 👥 ভিজিটর।
ডেটা প্রথম ভিজিটের কয়েক মিনিট পরে আসে। কেবল read-only; কিছুই বদলায় না।
