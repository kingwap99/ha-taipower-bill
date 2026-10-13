# Taipower Bill（台電電費）

Home Assistant 自訂整合。把家中電表／能源監測（例如 Emporia Vue）的用電度數，套進
**台灣電力公司的實際計費規範**，讓你隨時看到「這一帳期用了幾度、目前推定多少電費」，
並且可以回看歷期。

適用電價類別：**表燈 非時間電價**（一般住家）。支援多戶共用一個 HA，一戶一條設定即可
（例如兩戶共用一個電箱、各有自己的電表或 CT）。

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
每期固定費用、營業稅率、每月最低電費、一期月數。

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

每戶會生出 11 個感測器與 2 個控制項（`<base>` 是 HA 實際指派的 entity ID 前綴，中文裝置名
會被轉寫，**請在整合的「實體」頁複製實際 ID**）：

| Sensor | 意義 |
|---|---|
| `<base>_period_kwh` | 本期已累計用電（kWh） |
| `<base>_period_cost` | 本期推定電費（元） |
| `<base>_avg_daily_kwh` | 本期平均每日用電 |
| `<base>_projected_period_kwh` / `_projected_period_cost` | 照目前步調推估到抄表日 |
| `<base>_avg_price_per_kwh` | 當期每度平均電價（分租對帳直接用這個） |
| `<base>_marginal_price_per_kwh` | 下一度會落入的費率 |
| `<base>_days_remaining` / `<base>_next_read_date` | 距下次抄表天數／下次抄表日 |
| `<base>_bill_variance` | 上次結算時「推定 vs 實際帳單」的差額 |
| `<base>_closed_periods` | 已結算期數，歷史清單在它的 `history` 屬性 |

控制項：

- `number.<base>_manual_period_kwh` — 手動模式填本期度數
- `number.<base>_actual_bill` — 填實際帳單金額（對帳用）
- `button.<base>_close_period` — **收到電費單時按下去**：結算本期寫入歷史，並把抄見日
  自動推進 `間隔天數`，下一期立刻開始計

`_period_kwh` 與 `_period_cost` 宣告 `state_class: total` 並附上 `last_reset`＝本期起算日，
所以可以直接餵給能源面板的長期統計，結算時不會被記成一次巨大的負向變化。

### 屬性

`usage_source` 說明度數是怎麼來的：`statistics`（recorder 長期統計）、`live-delta`（退路）、
`manual`（手動）、`period-not-started`（本期還沒開始）或 `none`。另外
`stats_coverage_start`／`stats_last_day` 顯示統計涵蓋範圍，`stats_error` 只在統計讀取失敗時
出現並寫明原因，`config_issues`／`config_warning` 會指出設定上的問題。

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
內容包含當期進度條、跨季分攤說明、六段累進明細、以及近 8 期的歷史表。

---

## 服務

| 服務 | 用途 |
|---|---|
| `taipower_bill.close_period` | 結算本期寫入歷史。`force: true` 才允許在到期前提前結算 |
| `taipower_bill.set_last_read_date` | 覆蓋上期抄見日（未來日期會被拒絕），可同時清空歷史 |
| `taipower_bill.reset_ledger` | 只清歷史期別，不動設定 |

三個服務都可用 `target:` 指定裝置或實體，回傳值放在 `response_variable`。

---

## 疑難排解

### 數值幾乎都是空的／`days_elapsed` 是 0

**「上期抄見日」是未來日期。** 它一旦跑到未來，本期視窗就成了 `start > end`，所有數值都
無從計算（`days_elapsed=0`、`距下次抄表` 變成好幾百天）。

最常見的成因是 **「結算本期」被按了太多次** —— 每按一次就把抄見日往後推一個
`間隔天數`（60 天），按十次就是 600 天。

三種修法任選一種：

1. **UI**：整合 → 該 entry → `⋮` → **重新設定** → 把「上期抄見日期」改成電費單上的真實
   日期，並勾選「同時清除已結算歷史」（清掉那些憑空生出來的期別）→ 送出。
   填未來日期會被直接擋下來。
2. **服務**（可自動化）：
   ```yaml
   service: taipower_bill.set_last_read_date
   target:
     entity_id: sensor.taipower_a_period_kwh
   data:
     date: "2026-09-07"          # 電費單上的「上期抄見日期」
     reset_history: true
   ```
3. **砍掉重建**：刪除這條 entry 再重新新增（新 entry_id 等於新的 ledger 檔，歷史自然清空）。

修完之後 `config_issues` 會變空、`config_warning` 消失，數值立刻回來。

### 度數明顯偏低、`avg_price_per_kwh` 大得離譜

看一下 `usage_source` 屬性：

- **`live-delta`** — 統計路徑失敗，退回讀即時 state 的差值，度數會嚴重低估。
  屬性 `stats_error` 會寫明失敗原因（來源沒有長期統計、資料庫忙碌…）。
- **`period-not-started`** — 見上一節，抄見日在未來。
- **`none`** — 來源 entity 的 state 讀不到（entity 名稱改了？整合沒載入？）。

### Log 出現 state class 警告

HA 的 `DEVICE_CLASS_STATE_CLASSES` 規定 `energy` 只能用 `total`／`total_increasing`、
`monetary` 只能用 `total`。本整合自 v0.1.2 起只有真正在累計的兩個感測器宣告 `total`，
其他都是純量測值，不該再出現這類警告。

---

## 測試

兩套測試都是純 Python、不需要安裝 Home Assistant：

```bash
python3 -I tests/test_billing.py       # 計費引擎＋設定驗證（16 項）
python3 -I tests/test_coordinator.py   # 視窗／防呆／recorder 呼叫／state class（69 項）
```

`test_coordinator.py` 用一組最小的 HA stub 把 `coordinator.py` 真的跑起來，並且：

- **直接斷言傳進 recorder 的每個參數** —— 一次查全部來源、`period="day"`、
  `types={"change","sum"}`、`start_time`／`end_time` 就是視窗頭尾。這是抓出
  `end_time + 1 天` 這個 off-by-one 的方法。
- 驗證 `change` 缺漏時退回 `sum[-1]-sum[0]`、統計列的 `start` 是毫秒或秒都能解析、
  統計失敗會寫進 `stats_error` 且恢復後自動清掉。
- 照抄 HA 2026.10.1 `DEVICE_CLASS_STATE_CLASSES` 的規則，逐一檢查 11 個感測器的
  `(device_class, state_class)` 配對是否合法。

---

## 已知限制

- **只在 Home Assistant 2026.10.1 上實機驗證過。** 統計路徑依賴 recorder 的
  `statistics_during_period`；若你的 HA 版本較舊而這條路失敗，`stats_error` 會直接告訴你。
  `hacs.json` 的最低版本因此保守地設在 2026.10。
- **尚未在真實 HA 上驗證**：config entry 的 reconfigure／options 流程、entity registry 的
  實際掛載。首次載入若有 API 不符，看 Log 的 `taipower_bill` 那幾行即可定位；統計路徑的
  失敗一定會留下 WARNING，不會無聲無息。
- **附加費與營業稅沒有內建預設值**，必須照電費單填。
- 若 `period_start` 早於來源 entity 的統計起點，早於 `stats_coverage_start` 的度數不會被
  計入（卡片也會顯示）。今天那一列的統計還在累加中。
- `live-delta` 這條退路在來源歸零時會重取基準，會損失那一天的量；統計模式沒這個問題。

### 給開發者：HA recorder 統計 API 的四個坑

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

---

## 授權

MIT，見 [LICENSE](LICENSE)。
