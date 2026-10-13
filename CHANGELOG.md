# Changelog

## 0.1.2 — 2026-10-14

修掉三個「設定明明是對的，數字卻不對」的真實錯誤。

1. **recorder API 用錯（數值幾乎全空的真因）。** `statistics_during_period` 在 HA 2026.10
   是 `homeassistant.components.recorder.statistics` 的**模組層函式**，第一個參數是 `hass`
   —— 不是 `Recorder` 物件的方法。舊版以 `instance.statistics_during_period(...)` 呼叫會丟
   `AttributeError`，而那個例外被 `debug` 等級的 log 吞掉，於是每一次更新都安靜地退回
   `live-delta` 這條退路，看起來只是「數字偏低」。
   現在改為一次查全部來源、透過 `get_instance(hass).async_add_executor_job(...)` 執行；
   失敗會寫 WARNING，並把原因放進感測器屬性 `stats_error`，同時新增 `stats_last_day`
   顯示統計實際涵蓋到哪一天。
2. **視窗多夾一天。** HA 的 `end_time` 會**向上補到當日結束**，所以傳 `end + 1 天` 會多算
   一天。當期因為「明天還沒有資料」看不出來，但回看任何歷史期別都會多算。
3. **感測器 state class 非法組合。** 依 HA 的 `DEVICE_CLASS_STATE_CLASSES`，`energy` 只接受
   `total`／`total_increasing`、`monetary` 只接受 `total`、`date` 不接受任何 state class。
   原本 11 個感測器裡有 6 個宣告了 `measurement`，每次載入都會產生一則警告
   （HA 的 system_log 以格式字串去重，所以只留一條 `count=18`）。現在只有真正在累計的
   `period_kwh`／`period_cost` 宣告 `total`，並由 `last_reset` 提供本期起算日。

測試同時從 38 項擴充到 69 項：直接斷言傳進 recorder 的每一個參數（含 `end_time` 不得為
`end + 1 天`），並逐一驗證 11 個感測器的 `(device_class, state_class)` 配對合法。

## 0.1.1 — 2026-10-13

處理「上期抄見日跑到未來 → 全部數值空白」這個病徵。

- 新增 `validators.py`：填入的設定會在儲存前檢查（未來日期、過舊日期、格式錯誤、週期太短、
  戶別空白、來源為空），並在感測器屬性 `config_issues`／`config_warning` 上顯示。
- 「結算本期」不再能把抄見日推進未來（會被擋下並回報可讀的錯誤訊息），避免連按造成日期
  一次跳 600 天。
- 新增三個服務：`close_period`（可 `force`）、`set_last_read_date`（可 `reset_history`）、
  `reset_ledger`。
- 重新設定流程加入「同時清除已結算歷史」。

## 0.1.0 — 2026-10

初版。

- 台電表燈非時間電價計費引擎：雙月級距加倍、夏月 6/1–9/30、跨季按日數加權拆帳、
  費率改版獨立於季節、每期最低收費。
- 以「實際抄見日」為錨點的帳期視窗，一戶一條 config entry，來源 entity 可複選加總。
- 11 個感測器、2 個控制項、1 個結算按鈕，以及歷史 ledger。
- 讀取 HA recorder 長期統計（`statistics`）或手動輸入（`manual`）兩種用量來源。
- 儀表板卡片 `dashboard/taipower-bill-card.js`。
