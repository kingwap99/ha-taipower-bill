# Taipower Bill（台電電費）

Home Assistant 自訂整合。把家中電表／能源監測（例如 Emporia Vue）的用電度數，套進
**台灣電力公司的實際計費規範**，讓你隨時看到「這一帳期用了幾度、目前推定多少電費」，
並且可以回看歷期。

適用電價類別：**表燈 非時間電價**（一般住家）。支援多戶共用一個 HA，一戶一條設定即可
（例如兩戶共用一個電箱、各有自己的電表或 CT）。

> **v0.2.0 的帳期模型：一份「歷次抄見日」清單。**
> 一期的定義是 **（上一筆抄見日的隔天）→（這一筆抄見日）**。清單是唯一的真相來源，
> 所有視窗、歷史、統計都由它推導；按下按鈕只是「把今天這個抄見日寫進清單」的快捷。
> 因此**不是當天按、提前按、按錯、要改金額、要回看前幾個月**都可以事後調整，
> 而且可以重算。

---

## 它跟現成的整合差在哪

既有的 `taipower_bimonthly_cost`（cnstudio）也能算台電電費，但它對台電規範有幾處落差：

| 項目 | cnstudio 的做法 | 本整合 |
|---|---|---|
| 費率 | 寫死 2023 年以前的 `1.68/2.45/3.70/5.04/6.24/8.46` | `tariffs.py` 版本化費率表，依**生效日期**自動選表 |
| 跨夏月 | `now.month in [6,7,8,9]`，整期套同一種費率 | 依日數分攤：各季各算一次完整累進帳單，再按天數加權 |
| 帳期 | `(now - reset_day) % 60` 硬切，還會去改你的來源 entity | 以**實際抄見日**為錨點，每期在 UI 更新；完全不碰你的來源 entity |
| 費用組成 | 只有流動電費 | 含附加費（球金／空污）、營業稅、每期最低收費，並有結算與歷史 ledger |

另外它只吃一個 `utility_meter`；本整合可以**複選多個 kWh entity 加總**，所以你要把
「總電」和各迴路分開追蹤都行。

---

## 計費規則

實作依據（皆已用公開試算交叉驗證）：

1. **級距是「月」度數，雙月帳單門檻加倍**：`120 / 330 / 500 / 700 / 1000`
   　→ 一期（兩個月）為 `240 / 660 / 1000 / 1400 / 2000`。
2. **夏月是 6/1–9/30**（四個月，不是六到十月）。
3. **跨季不拆級距**。做法是：每一季各自用「整期總度數」算一次完整累進帳單，再依該季
   佔的天數加權。驗證案例：350 度、9/13–11/11（夏 18 天／非夏 42 天）→ **685.37 元**。
4. **費率改版獨立於季節**。跨 2025-10-01 改版的那一期，前後段會用不同費率
   （同一案例得 674.87 元，與整期套新表差約 10 元）。
5. **每期最低收費**。表燈非時間電價沒有契約容量基本費，但有每月最低 100 元；
   一期兩個月即 200 元。不足一期（例如中途才開始用本整合）會按 `⌊天數 ÷ 30.44⌋`
   比例降低門檻，所以一個 37 天的視窗最低是 100 元。
6. **目前費率表**（2025-10-01 生效，`元/度`）：

   | 級距 | 夏月 6/1–9/30 | 非夏月 |
   |---|---|---|
   | 1–240 度 | 1.78 | 1.78 |
   | 241–660 度 | 2.55 | 2.26 |
   | 661–1000 度 | 3.80 | 3.13 |
   | 1001–1400 度 | 5.14 | 4.24 |
   | 1401–2000 度 | 6.44 | 5.27 |
   | 2001 度以上 | 8.86 | 7.03 |

> 費率來源為公開整理頁面（台電官網是 JS 動態載入，無法直接解析）。**請拿一張實際電費單
> 核對後再信任**；若數字有出入，改 `custom_components/taipower_bill/tariffs.py` 即可。

---

## 安裝

### 用 HACS（自訂儲存庫）

1. HACS → 右上角三點 → **自訂儲存庫**，新增
   `https://github.com/kingwap99/ha-taipower-bill`，類別選 **Integration**。
2. 搜尋 **Taipower Bill** 安裝。
3. **重新啟動 Home Assistant**。
4. **設定 → 裝置與服務 → 整合 → 新增整合**，搜 `Taipower Bill`。

### 手動

1. 把 `custom_components/taipower_bill/` 整個複製到 HA 的 `config/custom_components/`。
2. 重新啟動 Home Assistant。
3. **設定 → 裝置與服務 → 整合 → 新增整合**，搜 `Taipower Bill`。

### 加掛儀表板卡片（選用）

把 `dashboard/taipower-bill-card.js` 複製到 `config/www/community/taipower-bill-card.js`，
然後 **設定 → 儀表板 → 右上角三點 → 自訂資源**，新增
URL `/local/community/taipower-bill-card.js`、類型 **JavaScript**。

**升級時卡片要另外換檔案**（它不在整合資料夾裡），換完在瀏覽器按一次
**Ctrl/Cmd + Shift + R** —— 自訂資源會被快取。卡片的版本寫在檔案開頭的註解裡
（`taipower-bill-card  (v0.2.2)`），可以拿它對照你有沒有換成功。

---

## 每戶要填什麼

| 欄位 | 說明 |
|---|---|
| 戶別名稱 | 例如 `A戶`、`B戶`。用來產生 unique_id，**多戶不可同名** |
| 用電量來源 | 該戶主進線的 kWh entity，**可複選加總**。選哪一個見下一節 |
| 用量取得方式 | `統計`＝讀 HA recorder 長期統計，可回溯；`手動`＝照電費單自填度數 |
| 上期抄見日期 | **準度的關鍵**。填電費單上的「上期抄見日」，本期起算日＝它的隔天 |
| 間隔天數 | 通常 60；若你的路線實際是 61/62 天，照實填 |
| 來源感測器（退路） | 統計讀不到時改讀即時 state 的 entity；留空則只用你指定的來源 |

「費率與附加費」在 **整合卡片 → 選項** 裡：夏月起迄、費率版本、每度附加費（球金＋空污）、
每期固定費用、營業稅率、每月最低電費、一期月數。「**通知目標**」也在這裡 ——
挑手機（`notify.mobile_app_*`，可複選），結算的通知就會帶「確認／取消」按鈕；
**留空＝不推**。

> **附加費預設 0，請照電費單上的實際金額填入**，否則算出來會比帳單低。

---

## 來源要選哪一個 entity

**選家裡那台能源監測「總電／主進線」channel 的 `Energy This Month` 感測器。**

以 Emporia Vue 為例，每個 channel 會生出三個 entity：
`Energy Today`（每日 00:00 歸零）、`Energy This Month`（每月 1 號歸零）、
`Power Minute Average`。四個實務上的踩雷點：

1. **不要靠 entity_id 判斷，要看 friendly name。** Emporia 整合在 entity_id 撞名時會加數字
   後綴，所以 **entity_id 的順序跟 channel 順序不一致** —— 名字裡沒有「總電」的那些很可能
   其實是某個迴路。唯一可靠的判斷法是打開裝置頁，看 friendly name 有沒有「總電」（或 `Total`）。
2. **`Energy This Month` 優先於 `Energy Today`。** 兩個都是裝置端就會歸零的計數器，本整合讀
   的是 HA 長期統計（跨越歸零仍是單調遞增）所以兩個都能用；但每日歸零每次都會漏掉一點，
   實測 200 天內兩者差距約 **0.5%**。月歸零的版本漏得少。
3. **來源是「累計、遞增」的計數器**最理想（`total_increasing` 或 `total`）。若你的是
   `utility_meter` 週期歸零型，本整合也吃得下（統計的 `sum` 會處理），但**不要**拿它的即時
   state 自己手算。
4. **可以自我驗證總電是否可信**：總電 ≈ 具名迴路加總 ＋ Balance（或 Unmonitored）。
   如果這個等式對得起來，代表主進線 CT 沒裝反、也沒有漏掉迴路，拿它當帳單來源就有意義。

其他要注意的：

- **長期統計有回溯上限。** HA recorder 的統計只從資料庫建立／開始累計那天起算。屬性
  `stats_coverage_start` 會告訴你實際起算日，早於那天的度數不會被計入（也就是說**裝機前
  的歷史期別無法回看**，除非你用手動模式填電費單上的度數）。
- 今天那一列的統計還在累加，屬性 `stats_last_day` 會顯示統計實際涵蓋到哪一天。

---

## 產生出來的 entity

每戶會生出 **17 個感測器、4 個按鈕、2 個 number**（`<base>` 是 HA 實際指派的 entity ID 前綴，
中文裝置名會被轉寫，**請在整合的「實體」頁複製實際 ID**）：

| Sensor | 意義 |
|---|---|
| `<base>_period_kwh` | 本期已累計用電（kWh） |
| `<base>_period_cost` | 本期推定電費（元） |
| `<base>_avg_daily_kwh` | 本期平均每日用電 |
| `<base>_projected_period_kwh` / `_projected_period_cost` | 照目前步調推估到抄表日 |
| `<base>_avg_price_per_kwh` | 當期每度平均電價（分租對帳直接用這個） |
| `<base>_marginal_price_per_kwh` | 下一度會落入的費率 |
| `<base>_days_remaining` / `<base>_next_read_date` | 距下次抄表天數／下次抄表日 |
| `<base>_bill_variance` | 上期結算時「推定 vs 實際帳單」的差額 |
| `<base>_closed_periods` | 已結算期數，歷史清單在它的 `history` 屬性 |
| `<base>_last_period_kwh` / `_last_period_cost` / `_last_period_avg_price` | **上一期**的度數／金額／平均電價 |
| `<base>_prev_period_kwh` / `_prev_period_cost` | **上上期**的度數／金額 |
| `<base>_pending_close_date` | 有待確認的結算時，那個待寫入的抄見日 |

控制項：

- `number.<base>_manual_period_kwh` — 手動模式填本期度數
- `number.<base>_actual_bill` — 填實際帳單金額（對帳用，結算時會被寫進那一期）
- `button.<base>_close_period` — **提出**一次結算（只提出，**不寫入**）
- `button.<base>_confirm_close` — 確認寫入。**沒有待確認的結算時是 `unavailable`**
- `button.<base>_cancel_close` — 丟掉這次的結算（本來就沒寫入任何東西）
- `button.<base>_undo_close_period` — **撤銷最後一期**（按錯結算的救援）。屬性 `will_remove`
  直接寫出會被移除的抄見日。**有待確認結算時它是 `unavailable`**（那時該按的是「取消」，
  所以它不會裝成一副能用的樣子），帳本是空的時候也一樣不給按。按鈕實體沒有辦法反問
  「你確定嗎？」，所以它把答案先寫在 `will_remove` 裡，要問就交給卡片或自動化。

`_period_kwh` 與 `_period_cost` 宣告 `state_class: total` 並附上 `last_reset`＝本期起算日，
所以可以直接餵給能源面板的長期統計，結算時不會被記成一次巨大的負向變化。

### 結算是「先提出、再確認」

HA 的服務沒有內建的確認步驟（只有 Lovelace 卡片的 `tap_action: confirmation:`，對自動化
無效），所以防誤按放在狀態機裡：

1. 按「結算本期」→ 只產生一筆**待確認**的結算：感測器屬性出現 `pending_close`、跳出一個
   persistent notification、`confirm`／`cancel` 兩顆按鈕變可用。**此時什麼都沒寫入。**
2. 15 分鐘內按「確認結算」（或呼叫 `taipower_bill.confirm_close`）才真的寫入。
3. 逾時就自動失效，不需要清理；按「取消結算」也可以直接丟掉。

真的按錯了還有救援：撤銷最後一期、改寫某一筆的日期／金額、重算已結算期別（見「服務」）。

**連「確認」都按錯的話**（手機在口袋裡被按到也算），有三條路可以退：

1. **手機通知上的按鈕**：提出結算時會推一則帶「確認寫入／取消」的通知，而**寫入成功之後
   同一則通知會換成「撤銷這一期」** —— 不用打開 Home Assistant 就能收回來。按鈕失效時
   （最常見的是提案已過 15 分鐘）會回一則寫明原因的訊息，不會靜默地什麼都不做。
   要在**選項 → 通知目標**裡挑手機（可複選）；**不填就不推**。下拉只列
   `notify.mobile_app_*`，因為只有 Companion App 的通知能帶按鈕 —— HA 核心的
   `persistent_notification` 沒有這個能力（它只收 message／title／notification_id）。

   **通知的兩個設計細節**：一戶**只有一則**通知（同一個 tag，新訊息取代舊的，所以鎖定畫面上
   永遠是最新狀態，不會留著一則按了卻說「沒有生效」的過期提案）；而且它會要求系統
   **留住它**（iOS：`time-sensitive`，穿透專注模式並留在鎖定畫面；Android：高重要性 channel、
   不設到期）。iOS 要在 **設定 → 通知 → Home Assistant → 時間敏感通知** 打開才會生效。

   > 想確認通知這條路有沒有通，做這個 30 秒測試就好：按 `button.<base>_close_period`
   > （**只提出，不寫入任何東西**）→ 手機會收到帶兩顆按鈕的通知 → 按「取消」。
   > 完全沒寫入帳本，可以放心按。
2. **`button.<base>_undo_close_period`**：實體按鈕，按下去就撤銷最後一期。
3. **卡片上的「撤銷最後一期（日期）」**：常駐在「歷次帳期」上方，按下去會先跳確認視窗並
   寫出**會移除哪個抄見日、本期會從哪天重新起算、要怎麼補回來**。

卡片的確認對話框是刻意設計的：**結算本身不問**（它只提出、不寫入任何東西 —— 在一個什麼都
沒做的地方問「你確定嗎？」，只會訓練你以後把真正重要的那個確認也按掉），而**「確認寫入」
與「撤銷」一定問，而且把日期與後果寫在問題裡**（「確定要撤銷最後一期？」不算確認，那叫
引誘 —— 你根本不知道要失去什麼）。

### 屬性

`usage_source` 說明度數是怎麼來的：`statistics`（recorder 長期統計）、`live-delta`（退路）、
`manual`（手動）、`bill-kwh`（用電費單上的度數補算歷史期）、`period-not-started`（本期還沒開始）
或 `none`。另外 `stats_coverage_start`／`stats_last_day` 顯示統計涵蓋範圍，`stats_error` 只在
統計讀取失敗時出現並寫明原因，`config_issues`／`config_warning` 會指出設定上的問題。

每個感測器都帶同一組屬性：`readings`（抄見日清單）、`history`（已結算期別）、`pending_close`、
`period_start`／`period_end`／`next_read_date`／`suggested_read_date`、`days_*`，
以及當期的分段明細（`tiers`／`segments`）。卡片就是讀這一包。

---

## 看板

```yaml
type: custom:taipower-bill-card
title: 台電電費
households:
  - entity: sensor.taipower_a_period_kwh
    label: A戶
  - entity: sensor.taipower_b_period_kwh
    label: B戶
```

卡片會依 `_period_kwh` 的後綴自動推導同一戶的其他 entity，所以只要指向 `period_kwh` 那一個。
內容包含當期進度條、跨季分攤說明、六段累進明細、歷史表（**可展開看全部期別**）、抄見日清單，
以及有待確認結算時的「確認寫入／取消」按鈕。

防誤按的部分長這樣：

- 有待確認結算時，那一塊用虛線框標出來，並寫明「**這筆結算還沒寫入**」、抄見日、本期度數、
  推定電費、**有效期限剩幾分鐘**。
- 沒有待確認結算、但帳本裡有抄見日時，常駐一列「**撤銷最後一期（日期）**」（虛線框），
  按下去先跳確認視窗。它不在歷史表裡面，不需要展開才找得到。
- 「確認寫入」與「撤銷」都會跳 `window.confirm`，並把**日期、度數、後果、如何補回來**
  寫進問題裡。

> 這一版修掉了一個從 v0.1.2 就存在的 bug：卡片把內容掛在 shadow root **外面**，
> 所以瀏覽器**從頭到尾一個字都沒畫**（沒有錯誤訊息、高度 0）。詳見 CHANGELOG 0.2.2。
> 換卡片檔案之後請 **Ctrl/Cmd + Shift + R** 一次。

---

## 服務

| 服務 | 用途 |
|---|---|
| `taipower_bill.close_period` | **提出**一次結算（`confirm: true` 則直接寫入）。到期前要 `force: true` |
| `taipower_bill.confirm_close` | 把待確認的結算寫入清單 |
| `taipower_bill.cancel_close` | 丟掉待確認的結算 |
| `taipower_bill.add_reading` | 直接寫入一個抄見日（**未來日期可以**，就是電費單上的「下次抄表日」） |
| `taipower_bill.undo_close_period` | 撤銷最後一期（或指定 `date`），那一期就回到「本期」 |
| `taipower_bill.update_reading` | 改寫已記錄的一筆：搬日期、改度數／金額、清金額 |
| `taipower_bill.period_report` | **任意期間回算明細**（`start`／`end`，或 `date` 指定某一期，`all: true` 全部） |
| `taipower_bill.set_last_read_date` | 覆蓋起始抄見日（未來日期會被拒絕），可同時清空歷史 |
| `taipower_bill.reset_ledger` | 清掉抄見日清單（並移除已發佈的長期統計） |

九個服務都可用 `target:` 指定裝置或實體，回傳值放在 `response_variable`。

幾個常見情境：

```yaml
# 1) 電費單上的下次抄表日是未來日期 -> 直接寫進去，帳期立刻以它為準
service: taipower_bill.add_reading
target: { entity_id: sensor.taipower_b_period_kwh }
data: { date: "2026-11-20" }

# 2) 三天前抄的表，今天才有空按 -> 提出結算時指定那一天（按鈕預設也會帶入推定抄見日）
service: taipower_bill.close_period
target: { entity_id: sensor.taipower_b_period_kwh }
data: { read_date: "2026-11-06" }

# 3) 按錯了 -> 撤銷最後一期
service: taipower_bill.undo_close_period
target: { entity_id: sensor.taipower_b_period_kwh }

# 4) 帳單金額來了 -> 補寫進那一期，並重算
service: taipower_bill.update_reading
target: { entity_id: sensor.taipower_b_period_kwh }
data: { date: "2026-11-06", actual_bill: 3120 }

# 5) 回看 6 月的明細
service: taipower_bill.period_report
target: { entity_id: sensor.taipower_b_period_kwh }
data: { start: "2026-06-01", end: "2026-06-30" }
response_variable: june
```

### 回看歷史的四條路

1. **HA 長期統計**：每一期都會用 `async_add_external_statistics` 寫入
   `taipower_bill:<戶別>_period_kwh` / `_period_cost` 兩條外部統計，所以在
   「統計」頁或能源面板就看得到歷期；改寫某期後會**整條重寫**（外部統計在同一時間戳是
   update，不是 append，所以不會變成兩條）。
2. **感測器**：`last_period_*`／`prev_period_*` 直接給上一期、上上期。
3. **`period_report` 服務**：任意期間回算明細（不必先結算過）。
4. **卡片**：歷史表可展開全部。

---

## 疑難排解

### 卡片是空白的（v0.2.1 以前一定是這樣）

v0.1.2 到 v0.2.1 的卡片有一個 bug：內容被掛在 shadow root **外面**，而 shadow host 上沒有
`<slot>`，所以瀏覽器**完全不會畫**，也不會報錯。**如果你那一版的卡片是一片空白，就是它**
—— 換上 v0.2.2 的 `dashboard/taipower-bill-card.js`（再 Ctrl/Cmd + Shift + R）就會出現。

要自己驗證的話，在瀏覽器開發者工具裡選到 `taipower-bill-card` 那個元素，看它的
`#shadow-root`：壞掉的版本裡面是空的，而 `childNodes` 卻有東西（東西都在不渲染的那一半）。

如果換了新卡片還是空白，依序檢查：

- 自訂資源的 URL 是 `/local/community/taipower-bill-card.js`，類型選 **JavaScript**；
  檔案真的在 `config/www/community/` 底下（`/local/` 對應的是 `config/www/`）。
- 瀏覽器有沒有硬重新整理（自訂資源會被快取）。
- 開發者工具的 Console 有沒有紅字；`type: custom:taipower-bill-card` 拼字是否正確。
- 卡片的 `entity:` 指向的是**存在**的那顆感測器（指向不存在的那一顆，卡片會直接寫
  「找不到實體」，而不是留白）。

### 手機沒收到結算通知／只有 Apple Watch 收到

依序看這幾點：

- **選項裡有沒有挑「通知目標」？** 沒挑就完全不推（預設是安靜的）。
- **只有手錶收到**：戴著 Apple Watch、手機鎖著時，iOS 只把通知送到手錶 —— 這是 iOS 的
  路由，不是整合的問題。iPhone 往下滑開「通知中心」通常還在；把手錶拿下來再測一次，
  手機就會跳出來。
- **通知一下就消失**：本版已把 payload 改成 `time-sensitive`（iOS）與高重要性 channel
  （Android）。iOS 還要在 **設定 → 通知 → Home Assistant → 時間敏感通知** 打開。
- **按了按鈕卻沒反應**：最常見的是提案已經過期（15 分鐘），這時會回一則
  「這個按鈕沒有生效：…」的通知說明原因；如果連那則都沒有，看 Log 裡 `taipower_bill`
  的 warning（`無法發送通知到 notify.…`），那代表目標服務名稱不對或裝置離線。
- 服務名稱要長得像 `notify.mobile_app_iphone`，可以在 **開發者工具 → 動作** 裡搜 `notify`
  確認實際名稱。

### 數值幾乎都是空的／`days_elapsed` 是 0

看屬性的 `config_issues`，它會直接告訴你哪一種：

- **`no_readings`** — 抄見日清單是空的。本期沒有任何起算點，全部數值都無從計算。
  按「結算本期」或呼叫 `add_reading` 寫入第一筆即可。
- **`no_current_reading`** — 清單裡的抄見日**全都還在未來**。這通常是誤把「下次抄表日」
  當成「上期抄見日」填進去了。`config_warning` 會把最早的那一筆秀出來。
- **`last_read_stale`** — 最後一筆已過的抄見日是兩個週期以前的事，提醒你該補新的了
  （如果你只是還沒收到電費單，忽略即可）。

修法（v0.2 之後都不必砍掉重建）：

```yaml
# 把某一筆的日期直接改對（例如本來填成未來）
service: taipower_bill.update_reading
target: { entity_id: sensor.taipower_a_period_kwh }
data: { date: "2027-01-05", new_date: "2026-09-07" }

# 或整份清單重新開始（連已發佈的長期統計一起清掉）
service: taipower_bill.reset_ledger
target: { entity_id: sensor.taipower_a_period_kwh }
```

UI 也可以：整合 → 該 entry → `⋮` → **重新設定** → 改「上期抄見日期」（填未來會被擋下）。

### 按鈕按了卻說「等於提前結算」

按鈕預設會帶入 **推定抄見日**（上一筆抄見日 + `間隔天數`）。如果電費單上的抄見日比推定值
早（或你想補記錄一個還沒到期的日期），兩種做法：

- 呼叫 `close_period` 時帶 `read_date` 指定那天，或
- 帶 `force: true`。

兩者都會在結算紀錄上留下 `forced` 標記，之後在歷史裡看得出來。

### 度數明顯偏低、`avg_price_per_kwh` 大得離譜

看一下 `usage_source` 屬性：

- **`live-delta`** — 統計路徑失敗，退回讀即時 state 的差值，度數會嚴重低估。
  屬性 `stats_error` 會寫明失敗原因（來源沒有長期統計、資料庫忙碌…）。
- **`period-not-started`** — 見上一節，抄見日在未來。
- **`bill-kwh`** — 那個歷史期別在長期統計裡沒有資料，改用電費單上的度數補算（會註明）。
- **`none`** — 來源 entity 的 state 讀不到（entity 名稱改了？整合沒載入？）。

### Log 出現 state class 警告

HA 的 `DEVICE_CLASS_STATE_CLASSES` 規定 `energy` 只能用 `total`／`total_increasing`、
`monetary` 只能用 `total`。本整合自 v0.1.2 起只有真正在累計的兩個感測器宣告 `total`，
其他都是純量測值，不該再出現這類警告。

---

## 測試

**兩套離線測試**：純 Python、不需要安裝 Home Assistant（需要 Python 3.11+）。

```bash
python3 -I tests/test_billing.py       # 計費引擎＋設定驗證（16 項）
python3 -I tests/test_coordinator.py   # 帳期／結算流程／recorder／外部統計／感測器（240 項）
```

**一套卡片測試**：跑真的卡片程式，**零外部依賴**（卡片是純 DOM，用一個 ~60 行的 shim
就足以真的跑它，所以不必 npm install 任何東西）。

```bash
node tests/test_card_render.mjs        # 渲染＋確認對話框（26 項）
```

**一套真機測試**：跑在**真的 Home Assistant 2026.10.1** 上（32 項）。

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements_test_ha.txt
pytest                                 # 等同 pytest tests/ha
```

`tests/ha/` 會自己把 repo 的 `custom_components/taipower_bill` 複製進一個拋棄式的 HA 設定
目錄，所以**不需要**任何指向 site-packages 的 symlink，也一定測到工作目錄裡的程式碼。它涵蓋
設定流程（新增戶別、重複戶別被擋、欄位驗證、重新設定、選項）、真的 config entry 設定、
entity registry、**活的 recorder 資料庫**（讀統計、寫外部統計、清除統計）、九個服務的完整流程
（提出→確認→撤銷、`add_reading`／`update_reading`／`period_report`／`set_last_read_date`／
`reset_ledger`），以及 v1→v2 的 store 遷移與「比程式新的儲存檔不會被覆蓋」。

`test_real_notify.py` 走的是**沒有辦法 stub 的那條路**：通知按鈕按下去之後，回到整合的
只有一個 `mobile_app_notification_action` **事件**（不是服務呼叫，也沒有通知的其他欄位），
所以那裡測的是「動作字串自己帶 entry id、整合只認自己的、別人家的 `CONFIRM_…` 不會動到
帳本」，以及失效的按鈕會回話、沒設定目標就完全不推。（Companion App 裝不進來，所以那個
`notify.mobile_app_*` 服務是測試自己註冊的，其餘全部是真的整合。）

`tests/test_card_render.mjs` 盯的是**看不到的失敗**：內容有沒有真的進 shadow root（見
「疑難排解 → 卡片是空白的」），以及每個會寫入帳本的動作是不是都先問過、而且問題裡有
日期與後果。它的第一項斷言就是把 shadow-DOM 那個 bug 重新植入時會紅的那一項。

> v0.2.0 就是死在這一層：離線測試全綠、CI 也全綠，真機上卻一個 entity 都生不出來
> （四個原因見 CHANGELOG 0.2.1）。根本問題是 **stub 比真貨寬鬆** —— 真 HA 會擋的呼叫，
> stub 照收。所以現在離線測試的 stub 只鏡射**真的**簽章（`Store` 的建構子、
> `Recorder.async_clear_statistics` 的位置、`HomeAssistant.loop`），真機那層則由
> `tests/ha/` 補上。

`test_coordinator.py` 用一組最小的 HA stub 把 `coordinator.py` 真的跑起來，並且：

- **直接斷言傳進 recorder 的每個參數** —— 一次查全部來源、`period="day"`、
  `types={"change","sum"}`、`start_time`／`end_time` 就是視窗頭尾。這是抓出
  `end_time + 1 天` 這個 off-by-one 的方法。
- **帳期算術**：抄見日清單 → 視窗、`days_elapsed`、未來的「下次抄表日」如何取代
  `間隔天數` 推定值、按晚三天時結算仍落在原本的抄見日。
- **結算流程**：提出時**什麼都不能寫入**（連 store 都不准存）、確認才寫、逾時失效、
  取消、撤銷、改寫／重算、重複日期的拒絕。
- **外部統計**：metadata 的 `mean_type`／`unit_class`／`statistic_id`、列的 `start` 必須是
  tz-aware 且落在整點、`sum` 為累積值、結束日在今天的視窗不得發佈。
- 照抄 HA 2026.10.1 `DEVICE_CLASS_STATE_CLASSES` 的規則，逐一檢查 17 個感測器的
  `(device_class, state_class)` 配對是否合法，並實例化每一顆確認 `last_reset` 只出現在
  `total` 感測器上、屬性包與卡片契約一致。

---

## 已知限制

- **只在 Home Assistant 2026.10.1 上實機驗證過。** 統計路徑依賴 recorder 的
  `statistics_during_period`；若你的 HA 版本較舊而這條路失敗，`stats_error` 會直接告訴你。
  `hacs.json` 的最低版本因此保守地設在 2026.10。
- **尚未在真實 HA 上驗證**：config entry 的 reconfigure／options 流程（`tests/ha/` 走的是
  「新增 entry → 載入」那條路）。entity registry 的實際掛載與九個服務都已在 `tests/ha/`
  用真的 HA 驗證過。首次載入若有 API 不符，看 Log 的 `taipower_bill` 那幾行即可定位。
- **附加費與營業稅沒有內建預設值**，必須照電費單填。
- 若 `period_start` 早於來源 entity 的統計起點，早於 `stats_coverage_start` 的度數不會被
  計入（卡片也會顯示）。今天那一列的統計還在累加中。
- `live-delta` 這條退路在來源歸零時會重取基準，會損失那一天的量；統計模式沒這個問題。
- **待確認的結算只存在記憶體裡**：HA 重啟就消失（反正它還沒寫入任何東西，重按一次即可）。
- 每戶的長期統計 id 由 `unique_id` 推導（`taipower_bill:<戶別>_period_kwh`）。改戶別名稱不會
  變動 id，但**刪掉 entry 再重建**會換 id，舊的那兩條統計要自己清。

### 給開發者：HA 2026.10 的 API 坑

以下每一條都是**對照 Home Assistant 2026.10.1 原始碼或真機**驗證過的，而且都真的讓 v0.2.0
在真機上陣亡（見 CHANGELOG 0.2.1）。要改這幾條路之前請先讀完。

#### a. 服務處理函式只收一個參數

HA 呼叫服務的方式是 `await target(service_call)` —— **恰好一個位置參數**：

```python
async def _handle_close_period(call: ServiceCall) -> dict:
    hass = call.hass          # not a parameter
```

寫成 `(hass, call)` 會得到 `TypeError: missing 1 required positional argument: 'call'`。
`ServiceCall.__slots__` 裡有 `context/data/domain/hass/return_response/service`，所以
`hass` 一律從 `call.hass` 拿；`HassJob` 不會注入它。

#### b. `Store` 沒有遷移參數，遷移要靠覆寫方法

`Store.__init__` 是
`Store(hass, version, key, *, minor_version=1, atomic_writes=False, encoder=None, decoder=None)`
——**沒有** `migrate_func`，也從來沒有 `async_migrate_func`。要遷移只能開子類別覆寫
`Store._async_migrate_func(old_major, old_minor, old_data)`；核心在
`Store._async_load_data` 裡比對 `version`／`minor_version`，不同才會呼叫它（未覆寫則丟
`NotImplementedError`），載完會自動 `async_save` 把新版寫回去。

#### c. `date` 感測器的 `native_value` 必須是 `date` 物件

平台會對 state 呼叫 `value.isoformat()`，回傳字串會炸
（`'str' object has no attribute 'isoformat'`）。**屬性**（`extra_state_attributes`）則相反，
要給 ISO 字串 —— 卡片讀的是屬性。

#### d. `async_clear_statistics` 在 `Recorder` 實例上，不在模組層

`homeassistant.components.recorder.statistics` 只有**阻塞版**的 `clear_statistics`；非阻塞版是
`Recorder.async_clear_statistics(statistic_ids, *, on_done=None)`，一個 `@callback`，透過
`get_instance(hass)` 取得。從模組層匯入會 `ImportError`，若被吞掉就變成「靜默不回傳」。

#### e. recorder 統計 API

以下對照 Home Assistant 2026.10.1 原始碼驗證過，改這條路之前請先讀：

1. `statistics_during_period` 是 `homeassistant.components.recorder.statistics` 的
   **模組層函式**，簽名為 `(hass, start_time, end_time, statistic_ids, period, units, types)`
   —— 不是 `Recorder` 的方法，第一個參數是 `hass`，且它會開 DB session，
   必須用 `get_instance(hass).async_add_executor_job(...)` 丟到 executor。
   （HA core 自己在 `recorder/websocket_api.py` 就是這樣呼叫的。）
2. **`end_time` 會被向上補到「它自己那一天」的結束**，所以視窗末日已經含在內。
   再 `+1 天` 會無聲無息多算一天 —— 當期看不出來（明天還沒有資料），但回看歷史期別就會錯。
3. 每日列的 `sum` 是**當日結束**時的累積值，所以要加總每一列的 `change`；
   用 `sum[-1] - sum[0]` 會漏掉第一天。
4. 統計列的 `start` 在 websocket 層是**毫秒**、在 in-process 呼叫是**秒**，兩者都要能吃。
5. 寫入外部統計的 `async_add_external_statistics(hass, metadata, statistics)` 是
   **`@callback`，不是協程**（呼叫時不要 `await`），它只是把 import job 塞進佇列。
   metadata 必須有 `has_mean`／`mean_type`（少了今天只跳 deprecation，**2026.11 會壞**）、
   `has_sum`、`source`、`statistic_id`、`unit_class`（要是已知的換算 key，kWh 用 `"energy"`；
   TWD 沒有對應的換算器，必須給 `None`，給 `"monetary"` 會被拒）。
6. 列的 `start` 必須是 **tz-aware 且落在整點**（recorder 在轉 UTC 前會檢查
   `minute == second == microsecond == 0`）。用 `dt_util.start_of_local_day(end + 1 天)`
   就是當地午夜，剛好符合。
7. 重送同一個 `start` 是 **update 而非 append**，所以「改寫已結算期別」可以直接整條重寫。

---

## 授權

MIT，見 [LICENSE](LICENSE)。
