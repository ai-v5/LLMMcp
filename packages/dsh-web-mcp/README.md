# llm-dsh-web-mcp

璁?DSH 鍘熺敓鐨?`web_search` / `web_fetch` 宸ュ叿**鏀圭敱鏈湴 LLMMcp MCP 鏈嶅姟鎻愪緵**鐨勬ˉ鎺ユ彃浠躲€?

DSH 鐨?`ctx.web` 鏄竴涓?provider 鎺ョ紳锛坄@deepseek-ai/dsh-web`锛夛細`@deepseek-ai/dsh-tool-web` 鍙礋璐ｆā鍨嬪彲瑙佺殑宸ュ叿锛岀湡姝ｆ绱?鎶撳彇鐢辨敞鍐岃繘鏉ョ殑 provider 瀹屾垚銆傛湰鎻掍欢鍚?`ctx.web` 娉ㄥ唽涓や釜 provider锛坄searchProvider` / `fetchProvider` 閮藉彨 `llmmcp`锛夛紝鍐呴儴閫氳繃 **stdio MCP** 璋?LLMMcp 鐨?`web_search` / `web_fetch`銆?

```
web_search 宸ュ叿 鈹€鈹?                       鈹屸攢 LLMMcpSearchProvider 鈹€鈹?
                 鈹溾攢 ctx.web (dsh-web) 鈹€鈹€鈹€鈹€鈹?                       鈹溾攢 MCP stdio 鈹€ python -m llmmcp
web_fetch  宸ュ叿 鈹€鈹?                       鈹斺攢 LLMMcpFetchProvider 鈹€鈹€鈹?
```

## 鐩綍

| 鏂囦欢 | 浣滅敤 |
|---|---|
| `index.js` | cordis 鎻掍欢锛坄name`/`inject`/`apply`锛? 鏈€灏?MCP stdio JSON-RPC 瀹㈡埛绔?+ 涓や釜 provider |
| `scripts/verify.mjs` | 鐢ㄥ亣 ctx 鐩存帴椹卞姩 provider锛?*鐪熷疄鎷夎捣 MCP 瀛愯繘绋?*锛坄--mock` 鐢ㄥ唴缃亣鏈嶅姟锛?|
| `scripts/verify-seam.mjs` | 鎸傝浇**鐪熷疄** `@deepseek-ai/dsh-web` 鏈嶅姟锛岃蛋 `ctx.web.search()` / `ctx.web.fetch()` |
| `scripts/check-profile.mjs` | 绂荤嚎鎸?launcher 璇箟閲嶅缓 profile 鐢熸晥鏉＄洰锛屾牎楠岃ˉ涓佹槸鍚︾湡鐨勭敓鏁?|
| `scripts/fake-mcp-server.mjs` | `--mock` 鐢ㄧ殑鍋?stdio MCP 鏈嶅姟锛堝惈閿欒/瓒呮椂/鏈煡宸ュ叿鐢ㄤ緥锛?|

## 瑁呴厤锛堟湰鏈哄凡灏变綅锛?

**1) 璁?profile 鑳芥寜瑁稿悕瑙ｆ瀽鎻掍欢**锛坄profiles/node_modules` 鏄悇 profile 鍏变韩鐨勮В鏋愬洖閫€鐩綍锛夛細

```powershell
New-Item -ItemType Junction `
  -Path   "$env:USERPROFILE\.dsh\profiles\node_modules\llm-dsh-web-mcp" `
  -Target "<repo>\packages\dsh-web-mcp"
```

`desktop` profile 鐢?pnpm 绠＄悊锛坄nodeLinker: hoisted`锛夛紝涔熷彲浠ユ敼鎴愬０鏄庡紡渚濊禆
`"llm-dsh-web-mcp": "link:<缁濆璺緞>"` 鍚?`pnpm install`锛涙湰鏈虹敤 junction 鏄负浜嗕笉瑙︾閿佹枃浠躲€?

**2) 在 `%DSH_HOME%\profiles\desktop\cordis.patch.yml` 末尾追加**（`%DSH_HOME%` 默认 `%USERPROFILE%\.dsh`）：

```yaml
- insert:
    - id: web-mcp
      name: llm-dsh-web-mcp
      config:
        providerId: llmmcp
        command: '<repo>\<llmmcp-python>'
        args: ['-m', 'llmmcp']
        cwd: '<repo>'
        toolCallTimeoutMs: 60000
        searchTimeoutS: 55
        fetchTimeoutS: 40
        warmup: true

- id: web
  name: '@deepseek-ai/dsh-web'
  config:
    searchProvider: llmmcp
    fetchProvider: llmmcp
```

瑕佺偣锛?
- `insert` 鏃?`id` 鏃?*杩藉姞鍒版牴鏉＄洰鍒楄〃**锛沗id: web` 鏄?`@deepseek-ai/dsh-base` 閲岀殑 `ctx.web` 鏈嶅姟鏉＄洰
  锛堝師鍊?`searchProvider: deepseek-official` / `fetchProvider: http`锛夈€?
- 琛ヤ竵璇箟鏄?*鏁翠綋鏇挎崲璇ユ潯鐩殑 `config`**锛岃€?`dsh-web` 鐨?config 鍙湁杩欎袱涓瓧娈碉紝鎵€浠ヤ笉浼氫涪閰嶇疆銆?
- patch 閲岀殑 `name` 蹇呴』涓庣洰鏍囨潯鐩竴鑷达紝鍚﹀垯璇ヨˉ涓佽璺宠繃锛堟棩蹇楁湁 `patch: name mismatch` 璀﹀憡锛夈€?
- `desktop` profile 鏈啓 `dsh.profile.patchReload` 鈫?榛樿 `live`锛氭敼 `cordis.patch.yml` 浼氱儹閲嶈浇锛?
  浣?*棣栨瑁呴厤/瑁?junction 鍚庤閲嶅惎 DSH**锛岀‘淇濇ā鍧楄В鏋愪笌 fiber 閮介噸鏂拌蛋涓€閬嶃€?

## 閰嶇疆椤?

| 瀛楁 | 榛樿 | 璇存槑 |
|---|---|---|
| `providerId` | `llmmcp` | 娉ㄥ唽鍒?`ctx.web` 鐨?provider id锛沗searchProvider`/`fetchProvider` 瑕佸啓瀹?|
| `command` | 鈥?锛堝繀濉級 | MCP 鏈嶅姟鍙墽琛屾枃浠讹紱Windows 鐢?venv 閲?`python.exe` 鐨?*缁濆璺緞** |
| `args` | `[]` | 渚嬶細`['-m','llmmcp']` |
| `cwd` | 杩涚▼ cwd | 瀛愯繘绋嬪伐浣滅洰褰曪紱寤鸿鎸囧埌 LLMMcp worktree 鏍?|
| `env` | `{}` | 杩藉姞鐜鍙橀噺锛堣鐩栫埗鐜锛夈€傜埗鐜閲?`*KEY*/*TOKEN*/*SECRET*/*PASSWORD*/*AUTH*` 褰㈢姸鐨勫彉閲?*涓嶄細**浼犵粰瀛愯繘绋嬶紱浠ｇ悊鍙橀噺浼氭甯镐紶閫?|
| `searchTool` | `web_search` | LLMMcp 鎼滅储宸ュ叿鍚?|
| `fetchTool` | `web_fetch` | LLMMcp 鎶撳彇宸ュ叿鍚?|
| `toolCallTimeoutMs` | `60000` | 鍗曟 `tools/call` 鐨勪紶杈撳眰棰勭畻 |
| `searchTimeoutS` | 娲剧敓 `min(55, toolCallTimeoutMs/1000-5)` | 浼犵粰 `web_search` 鐨?`timeout`锛堢锛夈€?*蹇呴』鏄惧紡缁?*锛歀LMMcp 渚ч粯璁?25s锛岃€?Google 棣栨煡瑕佹覆鏌撴祻瑙堝櫒 |
| `fetchTimeoutS` | 娲剧敓 `min(40, toolCallTimeoutMs/1000-5)` | 浼犵粰 `web_fetch` 鐨?`timeout`锛堢锛?|
| `warmup` | `true` | 鎻掍欢鍔犺浇鏃跺氨鎷夎捣瀛愯繘绋嬪苟 `tools/list`锛岄伩鍏嶉娆″伐鍏疯皟鐢ㄧ瓑瑙ｉ噴鍣ㄥ惎鍔?|
| `fetchFormat` | `html` | 浼犵粰 `web_fetch` 鐨?`format`锛沗dsh-tool-web` 鑷繁鎶?HTML 杞?markdown 骞跺仛闀垮害鎺у埗 |
| `fetchMaxChars` | `400000` | 浼犵粰 `web_fetch` 鐨?`max_chars`锛堝ぇ浜庡伐鍏峰眰 200k 杈撳嚭涓婇檺锛岄伩鍏?provider 渚у厛鎴柇锛?|
| `protocolVersion` | `2025-06-18` | `initialize` 璇锋眰鐨勫崗璁増鏈紝灞?legacy 鎻℃墜鐗堟湰锛堟崲琛屽垎闅?JSON锛?|
| `clientName` / `clientVersion` | `dsh-web-mcp` / `0.1.0` | 涓婃姤缁?MCP 鏈嶅姟鐨勫鎴风淇℃伅 |
| `stderrTailChars` | `4000` | 瀛愯繘绋?stderr 淇濈暀闀垮害锛屽彧鐢ㄤ簬閿欒娑堟伅 |

**涓嶅鍑?schemastery `Config`**锛歚@deepseek-ai/schemastery` 鍙兘浠?harness 渚濊禆鍥捐В鏋愬埌锛?
鎻掍欢鍦?profile 閲屾槸 `link:` 杩涙潵鐨勯浂渚濊禆鍖咃紱鍦ㄦā鍧楅《灞?import 瀹冧細鍥犺В鏋愬け璐ヨ€屾暣琛屾彃浠跺姞杞藉け璐ャ€?
閰嶇疆鍦?`resolveOptions()` 閲屾牎楠岋紝瀛楁閿欎細鎶涘嚭甯﹀瓧娈靛悕鐨勯敊璇€?

## 楠岃瘉

```powershell
cd <repo>\packages\dsh-web-mcp

node scripts/verify.mjs --mock --checks   # 涓嶉渶瑕?Python锛氶敊璇?瓒呮椂/鍙栨秷/澶嶇敤鍏ㄩ摼璺?
node scripts/verify.mjs                   # 鐪熷疄 LLMMcp锛歴earch 鏉℃暟+棣栨潯鏍囬銆乫etch 鐘舵€?闀垮害銆佽€楁椂
node scripts/verify-seam.mjs              # 鎸傜湡瀹?ctx.web 鏈嶅姟锛歝tx.web.search()/fetch()
node scripts/check-profile.mjs            # 鍙锛氭鏌?profile 琛ヤ竵鐪熺殑鐢熸晥銆佹彃浠跺彲瑙ｆ瀽
```

棰勬湡锛堟湰鏈哄疄娴嬶紝2026-10-20锛夛細

```
timeouts:   search=55s fetch=40s
search("ascend npu mfu", 5)   sources: 5   elapsed: ~11-13 s
fetch("https://example.com")  status: 200  body len: 577  elapsed: ~0.6 s
second fetch (connection reuse): ~0.1 s
result: OK
```

`check-profile.mjs` 鍦ㄦ墦鍖呭畨瑁呯幆澧冧笅浼氭彁绀鸿嫢骞?bundle 灞?offline 涓嶅彲璇?锛堝畠浠湪 `app.asar` 閲岋級锛?
杩欑被 `patch: entry ... not found` 璀﹀憡涓嶇畻澶辫触锛沗web-mcp` 涓?`web` 涓や釜鏉＄洰蹇呴』鍑虹幇涓?config 姝ｇ‘銆?

## 鍥炴粴

```powershell
# 1) 鎭㈠琛ヤ竵锛堟敼鍧忎簡灏辩敤澶囦唤锛涘浠藉悕 cordis.patch.yml.bak-<时间>锛?
Copy-Item -LiteralPath "$env:USERPROFILE\.dsh\profiles\desktop\cordis.patch.yml.bak-<时间>" `
          -Destination "$env:USERPROFILE\.dsh\profiles\desktop\cordis.patch.yml" -Force

# 2) 鍗歌浇鎻掍欢杞摼锛堝彲閫夛級
Remove-Item -LiteralPath "$env:USERPROFILE\.dsh\profiles\node_modules\llm-dsh-web-mcp"

# 3) 閲嶅惎 DSH
```

鏈€灏忓洖婊?= 鍒犳帀 `cordis.patch.yml` 閲?`web-mcp` 涓?`id: web` 涓や釜鏉＄洰鍚庨噸鍚細`ctx.web` 鍥炲埌
`deepseek-official` / `http`锛屽師鐢熷伐鍏疯涓哄畬鍏ㄦ仮澶嶏紙鎻掍欢鏈韩涓嶅啓浠讳綍鎸佷箙鐘舵€侊級銆?
涔熷彲浠ョ敤鐜鍙橀噺涓存椂鍏滃簳锛歚DSH_WEB_SEARCH_PROVIDER` / `DSH_WEB_FETCH_PROVIDER`锛堜絾 config 浼樺厛绾ф洿楂橈紝
涓よ€呭悓鏃跺瓨鍦ㄦ椂浠?`cordis.patch.yml` 涓哄噯锛夈€?

## 鏁呴殰鎺掓煡

| 鐜拌薄 | 澶勭悊 |
|---|---|
| `web_search` 棣栨 10~20 s | 姝ｅ父锛欸oogle 缁撴灉椤靛鏅€?HTTP 瀹㈡埛绔槸 JS 闂ㄧ锛孡LMMcp 鐢?headless 娴忚鍣ㄦ覆鏌擄紝鑰楁椂闅忔満鍣ㄨ礋杞藉彉鍖?|
| `GOOGLE_RENDER_FAILED: headless browser timed out` | 鍏?`llmmcp diagnose`锛坄python -m llmmcp diagnose`锛夌湅娴忚鍣?浠ｇ悊锛涘啀鏌ユ槸鍚︽湁娈嬬暀 `chrome/msedge` 杩涚▼锛涘繀瑕佹椂鎶?`searchTimeoutS` 鎻愬埌 55锛堝凡榛樿锛?|
| `SEARCH_ALL_FAILED` 涓?bing 鎶ョ浉鍏虫€у畧鍗?| 鏌ヨ璇嶅お娉?澶暱锛屾崲鏇村叿浣撶殑鏌ヨ锛涜繖鏄?LLMMcp 寮曟搸閾剧殑琛屼负锛屼笉鏄ˉ鎺ラ棶棰?|
| 宸ュ叿鎶?`configured web provider "llmmcp" is not registered` | 鎻掍欢娌″姞杞芥垚鍔燂細鐪?DSH 鏃ュ織閲?`web-mcp` 琛岋紱鏍稿 `name: llm-dsh-web-mcp` 鑳藉惁浠?profile 鐩綍 `require.resolve` |
| 宸ュ叿鎶?`registered but unavailable` | `available()` 鍙仛鏈湴鍒ゆ柇锛歚config.command` 涓嶅瓨鍦ㄦ垨涓嶅彲鎵ц 鈫?鏍稿 venv python 缁濆璺緞 |
| `LLMMcp warmup failed ...` | 瀛愯繘绋嬭捣涓嶆潵锛氱敤鍚屼竴 `command`/`args`/`cwd` 鎵嬭窇 `python -m llmmcp` 鐪?stderr锛涢敊璇枃鏈細甯?stderr 灏鹃儴 |
| 鎯崇‘璁ゅ疄闄呯敤浜嗗摢涓?provider | DSH 鏃ュ織涓?`ctx.web now has providers "llmmcp" ...`锛屾垨 `LMMcp MCP session ready (server=llmmcp ...)` |

**stdout 绾緥**锛氭彃浠朵粠涓嶅啓 stdout锛坰tdout 灞炰簬瀹夸富锛夛紝瀛愯繘绋?stdout 鍙蛋 JSON-RPC锛涙墍鏈夋棩蹇楄蛋
`ctx.logger`锛宻tderr 灏惧反鍦ㄥ啓杩涙棩蹇?閿欒鍓嶄細鎶?`user:password@` 鎶规垚 `***@`銆?

## 宸茬煡杈圭晫

- **闈?2xx 璇箟**锛氬唴缃?http provider 鎶婇潪 2xx 褰?缁撴灉"锛孡LMMcp 鍒欐姏 `FetchError`锛屾墍浠ユˉ鎺ヤ細**鎶ラ敊**
  鑰屼笉鏄繑鍥?`statusCode`锛涢渶瑕佺姸鎬佺爜鏃剁湅閿欒鏂囨湰閲岀殑 code/鐘舵€併€?
- **鍗曡繛鎺ヤ覆琛?*锛氫竴涓瓙杩涚▼銆佷竴涓?`tools/call` 闃熷垪锛涘苟鍙?`web_search` 浼氭帓闃燂紙DSH 宸ュ叿鏈韩涔熸湁 60s 棰勭畻锛夈€?
- **瀛愯繘绋嬪洖鏀?*锛歚close()` 鍏堝叧 stdin锛?50 ms 鍐呮湭閫€鍑哄氨 `taskkill /T /F`锛坴env 鐨?`python.exe` 鍙兘鏄?
  鍚姩鍣紝鐪熸鐨勮В閲婂櫒鏄畠鐨勫瓙杩涚▼锛屽彧 kill 鐩存帴瀛愯繘绋嬩細鐣欏鍎匡級锛涘涓荤‖閫€鍑烘椂鐢?`process.once('exit')`
  鍏滃簳鍚屾 kill銆?
- **璺ㄧ洏 junction**锛氭彃浠舵湰浣撳湪 `D:`锛宲rofile 鍦?`C:`锛涙彃浠跺寘闆朵緷璧栵紝鎵€浠ヤ笉闇€瑕?hoist 浠讳綍鍖呭埌 profile銆?
