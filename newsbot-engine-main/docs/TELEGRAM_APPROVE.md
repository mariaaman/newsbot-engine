# টেলিগ্রামে অনুমোদন, বাতিল ও সম্পাদনা

সংবেদনশীল (sensitive) খসড়া এখন `pending/`-এ পড়ে থাকে না, বরং টেলিগ্রামে আসে। মেসেজে থাকে:

- **✅ অনুমোদন** আর **❌ বাতিল** বাটন
- **👁 দেখুন ও সম্পাদনা** লিংক: একটা ব্যক্তিগত পাতা, যেখানে খসড়া পড়া যায়, শিরোনাম, সারাংশ ও লেখা ঠিক করা যায়, তারপর **সংরক্ষণ**, **অনুমোদন ও প্রকাশ** বা **বাতিল**

অনুমোদন দিলে খসড়াটা `pending/` থেকে `content/`-এ চলে যায় (`pending:` অংশ মুছে), আর `deploy` workflow সাইট প্রকাশ করে। সাধারণত ১–৩ মিনিট লাগে। বাতিল করলে খসড়া মুছে যায়।

## নিয়ম

- **মান-যাচাইয়ে ব্যর্থ খসড়ায়** (অচেনা নাম, সূত্রহীন সংখ্যা, অনেক ছোট লেখা...) মেসেজে অনুমোদন বাটন থাকে না, শুধু বাতিল আর লিংক। লিংকের পাতায় নিজে সব যাচাই করে টিক দিলে তবেই অনুমোদন চলে।
- সম্পাদনার পর লেখা আবার মেশিন-যাচাইয়ে যায় না। শেষ সিদ্ধান্ত আপনার, তাই নাম, সংখ্যা আর উদ্ধৃতি নিজে মিলিয়ে নিন।
- লিংকটা একটা গোপন চাবির মতো। যার কাছে লিংক আছে সে ওই এক খসড়া সম্পাদনা বা অনুমোদন করতে পারে। লিংক শুধু আপনার প্রাইভেট চ্যাটে যায়, কাউকে ফরোয়ার্ড করবেন না। খসড়া নিষ্পন্ন হলে লিংক আপনা-আপনি অকেজো। বাড়তি নিরাপত্তা চাইলে নিচের `EDIT_PIN` দিন।
- পুরনো `pending/`-এর খসড়াগুলোর জন্য মেসেজ আর আসবে না। শুধু এখন থেকে নতুন তৈরি হওয়া খসড়ার মেসেজ আসবে।
- PR-ভিত্তিক (সংবেদনশীল নয়) লেখার প্রবাহ আগের মতোই: **PR খুলুন** বাটন।

## সেটআপ: স্বয়ংক্রিয় (সহজ পথ)

Cloudflare-এ হাতে কিছু বানাতে হয় না। `setup-approve` workflow Worker বানায়, সিক্রেট বসায়, টেলিগ্রামের webhook ঠিক করে, আর brain কনফিগে দুটো লাইন নিজেই বসিয়ে দেয়।

**ক. Cloudflare টোকেনে অনুমতি যোগ করুন (একবার)**
Cloudflare dashboard, Manage Account, **Account API Tokens** (বা My Profile, API Tokens), আপনার বর্তমান টোকেনের পাশে **Edit**। Permissions-এ যোগ করুন: **Account, Workers Scripts, Edit**। Save করুন। টোকেনের মান বদলায় না, তাই GitHub-এর সিক্রেট ঠিকই থাকে।

**খ. নতুন GitHub টোকেন বানিয়ে সিক্রেট হিসেবে রাখুন**
GitHub, Settings, Developer settings, Fine-grained tokens, Generate new token। Only select repositories: `newsbot-brain` ও `newsbot-engine`। Permissions: **Contents: Read and write**, **Actions: Read and write**। টোকেন কপি করে engine রিপোর Settings, Secrets and variables, Actions, **New repository secret**-এ নাম `APPROVE_GH_TOKEN` দিয়ে রাখুন।

**গ. একবার Workers & Pages খুলে দেখুন**
Cloudflare dashboard-এ Workers & Pages খুলুন। `workers.dev` subdomain বাছাই করতে বললে একটা নাম দিন। আগে থেকে থাকলে কিছু করতে হবে না।

**ঘ. workflow চালান**
engine রিপোর Actions, বাম তালিকায় **setup-approve**, **Run workflow**। সফল হলে লগে ধাপগুলো `ok` দেখাবে আর টেলিগ্রামে "অনুমোদন চালু হয়েছে" মেসেজ আসবে। লগ পাবলিক, তাই সেখানে শুধু `ok` বা `fail:কারণ` লেখা থাকে, ঠিকানা বা সিক্রেট কখনো নয়।

এরপর ধাপ ৫ (`hourly` চালিয়ে পরীক্ষা) পর্যন্ত বাকি সব নিচের হাতে-করা অংশ বাদ দিয়ে এগোতে পারেন।

## সেটআপ: হাতে (বিকল্প, স্বয়ংক্রিয়টা না চললে)

### ১. GitHub টোকেন (Worker-এর জন্য)
GitHub, Settings, Developer settings, Personal access tokens, **Fine-grained tokens**, Generate.

- Resource owner: আপনার অ্যাকাউন্ট
- Repository access: **Only select repositories**, `newsbot-brain` আর `newsbot-engine`
- Permissions: **Contents: Read and write**, **Actions: Read and write**
- মেয়াদ: আপনার পছন্দমতো (শেষ হলে নতুন বানিয়ে Worker-এ বদলাবেন)

টোকেনটা কপি করে রাখুন।

### ২. Cloudflare Worker বানান
Cloudflare dashboard, **Workers & Pages**, Create, **Create Worker**, নাম `newsbot-approve`, Deploy। তারপর **Edit code** খুলে আগের কোড মুছে `worker/index.js`-এর পুরো লেখা পেস্ট করুন, **Deploy**।

Settings, **Variables and Secrets**-এ যোগ করুন:

| নাম | ধরন | মান |
|---|---|---|
| `BRAIN_REPO` | Text | `আপনার-ইউজারনেম/newsbot-brain` |
| `ENGINE_REPO` | Text | `আপনার-ইউজারনেম/newsbot-engine` |
| `GH_TOKEN` | Secret | ধাপ ১-এর টোকেন |
| `TELEGRAM_BOT_TOKEN` | Secret | GitHub-এ যে বট টোকেন দিয়েছেন সেটাই |
| `TELEGRAM_CHAT_ID` | Secret | GitHub-এ যে চ্যাট আইডি দিয়েছেন সেটাই |
| `WEBHOOK_SECRET` | Secret | নিজে বানানো একটা লম্বা র‍্যান্ডম লেখা (শুধু ইংরেজি অক্ষর, সংখ্যা, `_` বা `-`) |
| `EDIT_PIN` | Secret, **ঐচ্ছিক** | একটা PIN। দিলে সংরক্ষণ, অনুমোদন ও বাতিলে PIN লাগবে |

Worker-এর ঠিকানা হবে `https://newsbot-approve.<আপনার-subdomain>.workers.dev`। এটা ব্রাউজারে খুললে `ok` দেখাবে।

### ৩. টেলিগ্রামকে Worker-এর ঠিকানা জানান
ব্রাউজারে এই লিংক একবার খুলুন (বড় অক্ষরের জায়গাগুলো নিজের মান দিয়ে বদলান):

```
https://api.telegram.org/bot<বট-টোকেন>/setWebhook?url=https://newsbot-approve.<subdomain>.workers.dev/telegram&secret_token=<WEBHOOK_SECRET>&allowed_updates=["callback_query"]
```

`"ok":true` এলে হয়েছে। যাচাই: একই ঠিকানায় `setWebhook`-এর জায়গায় `getWebhookInfo` লিখুন। `last_error_message` ফাঁকা থাকা উচিত। এই লিংকে বট টোকেন থাকে, তাই কাজ শেষে ব্রাউজারের ইতিহাস থেকে মুছে দিন।

### ৪. ইঞ্জিন আপডেট
এই প্যাচ zip engine রিপোতে আপলোড করুন, `ci` সবুজ হতে দিন। তারপর **brain** রিপোর `config/newsbot.yaml`-এর `notify: telegram:` অংশে দুটো লাইন বসান:

```yaml
    approve_buttons: true
    edit_url: "https://newsbot-approve.<subdomain>.workers.dev"
```

### ৫. পরীক্ষা
Actions, **hourly**, Run workflow। কোনো সংবেদনশীল খসড়া তৈরি হলে মেসেজ আসবে। লিংকটা খুলে দেখুন, ছোট একটা সম্পাদনা করে **সংরক্ষণ** দিন, তারপর **অনুমোদন**। Actions-এ **deploy** চলবে।

## কীভাবে কাজ করে
- বাটন চাপলে টেলিগ্রাম Worker-কে খবর দেয়। Worker শুধু আপনার চ্যাট আইডি থেকে আসা বাটন মানে, `WEBHOOK_SECRET` মিলিয়ে।
- লিংকের শেষের `s=` একটা HMAC স্বাক্ষর (বট টোকেন থেকে বানানো চাবি, তাই আলাদা সিক্রেট লাগে না)। বট টোকেন বদলালে পুরনো লিংক অকেজো হবে।
- Worker brain রিপোতে একটাই commit করে: খসড়া সরে যায় বা মুছে যায়। তারপর `deploy.yml` চালু করে। এটা `hourly`-র একই সারিতে দাঁড়ায়, তাই ওদের মধ্যে ধাক্কা লাগে না।
- Deploy ব্যর্থ হলে GitHub আপনাকে ইমেইল করে। অনুমোদিত লেখা brain-এ থেকে যায় আর পরের `hourly` রানে প্রকাশ হয়।
