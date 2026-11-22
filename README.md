# Literature Knowledge Graph — Auto-Sync to Cloudflare Pages

定时将 Google Drive 中的两套只读文献互动知识网增量同步并部署为两个独立的 [Cloudflare Pages](https://developers.cloudflare.com/pages/platform/limits/) 静态网站：

| 网站名称 | Cloudflare Pages 项目名 | 默认访问网址 | Drive 源目录 ID |
| :--- | :--- | :--- | :--- |
| **Literature on Middle East Studies** | `literature-on-middle-east-studies` | `https://literature-on-middle-east-studies.pages.dev` | `1to2zAmb3zlPWHYNMvfFXzMm4vxv1Eh-k` + `shards_me` (`19mKtSIECDc_t-XYfTflHgT0Uy-_Tafnt`) |
| **Literature on Autism Research** | `literature-on-autism-research` | `https://literature-on-autism-research.pages.dev` | `1V1dsevYYd0-CxHfaWo-Fhig3vj1fDuPM` + `shards_autism` (`1KZFYwXcr3eW-H0E52C3NTnv79PPmx-v9`) |

## 核心设计

* **面向 10 万+ 文献规模**：通过 Google Drive API v3 分页接口（`pageSize=1000` + `nextPageToken`）读取分片，规避公开链接抓取的 50 文件上限；结合 `actions/cache` 与 `md5Checksum` 增量比对，已封存分片不重复下载、无更新时不触发部署。
* **零 Git 仓库膨胀**：不将分片文件提交进 GitHub 仓库，由 GitHub Actions 临时拉取后直接通过 [Wrangler Direct Upload](https://developers.cloudflare.com/pages/get-started/direct-upload/) 推送上线。
* **全局只读**：部署产物仅包含 `index.html`、`shards_*/*.js`、`shard_manifest.json` 与 `_headers`，自动跳过 `.json.gz` 主库与 `.py` 构建脚本。

## 所需 GitHub Secrets（Settings → Secrets and variables → Actions）

1. `GOOGLE_SERVICE_ACCOUNT_JSON`：Google Cloud 只读 Service Account 的 JSON 密钥全文（支持原文或 Base64）。
2. `CLOUDFLARE_API_TOKEN`：具有 `Account → Cloudflare Pages → Edit` 权限的 Cloudflare API Token。
3. `CLOUDFLARE_ACCOUNT_ID`：Cloudflare 控制台右侧栏显示的 Account ID。

## 定时频率

默认按 `15 */3 * * *`（每 3 小时第 15 分钟 UTC）自动运行，紧跟每 3 小时 `:45` / `:55` 的网页构建任务；也可在仓库 **Actions** 页面点击 **Run workflow** 手动触发（勾选 `force_deploy` 可强制重新部署）。
