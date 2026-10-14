# Changelog

## 0.2.1 — 2026-10-14

**v0.2.0 裝上去完全不能用** —— 這版把「在真的 Home Assistant 上跑不起來」的四個原因修掉，
並且補上一套**跑在真的 Home Assistant 2026.10.1 上**的測試，讓這種事不會再發生。

v0.2.0 的離線測試全綠、GitHub Actions 也全綠，卻在真機上一個 entity 都生不出來。原因是
那些測試把 Home Assistant **stub 掉**了，而 stub 比真貨寬鬆：真貨會擋的呼叫，stub 照收。
以下四點，每一點都是「stub 可以，真 HA 不行」。

### 1. 設定檔初始化就爆：`Store` 已經沒有遷移參數（真因）

HA 2026.10 的 `Store.__init__` 是
`Store(hass, version, key, *, minor_version=1, atomic_writes=False, encoder=None, decoder=None)`
——**沒有任何遷移參數**（舊的 `migrate_func` 已被移除，`async_migrate_func` 則從未存在）。
v0.2.0 仍傳了 `async_migrate_func=`，於是 `TypeError` 在協調器建構子裡炸開，
`async_setup_entry` 失敗 —— **整個整合一個 entity 都沒有**。

遷移現在走核心唯一允許的路：在子類別覆寫 `Store._async_migrate_func`（`TaipowerLedgerStore`），
`Store._async_load_data` 會在版本不同時呼叫它；遷移邏輯本身仍留在模組層函式
`_async_migrate_store`，所以離線可測。

> 為什麼沒被測到：測試裡的假 `Store.__init__` 收了 `async_migrate_func=None, **kwargs`。
> 這是**假貨比真貨寬鬆**造成的事故，現在假 `Store` 的簽章與真貨一致，並額外斷言
> 「未覆寫 `_async_migrate_func` 的 `Store` 會丟 `NotImplementedError`」。

### 2. 九個服務全部呼叫失敗：服務處理函式的參數個數

HA 2026.10 呼叫服務是 `await target(service_call)` —— **恰好一個參數**。v0.2.0 的處理函式
簽章是 `(hass, call)`，所以每一個服務都死在
`TypeError: missing 1 required positional argument: 'call'`（`hass` 要從
`ServiceCall.hass` 拿）。九個服務（`close_period`、`confirm_close`、`cancel_close`、
`add_reading`、`undo_close_period`、`update_reading`、`period_report`、
`set_last_read_date`、`reset_ledger`）現在都是 `(call: ServiceCall)`。

### 3. 兩顆日期感測器會讓平台拋錯

HA 2026.10 的 `date` 感測器會對狀態呼叫 `value.isoformat()`，所以 `native_value` 必須回傳
**真正的 `date` 物件**。v0.2.0 回傳的是已經格式化好的字串，平台於是在
`'str' object has no attribute 'isoformat'` 上炸掉。現在 `native_value` 回傳 `date`，
而**屬性**（`extra_state_attributes`）維持 ISO 字串給卡片用 —— 兩者需求不同。

### 4. 清除統計的 API 位置錯了，而且錯得很安靜

`async_clear_statistics` 不是 `recorder.statistics` 的模組層函式（那裡只有阻塞版的
`clear_statistics`），它是 **`Recorder` 實例上的 `@callback`**：
`get_instance(hass).async_clear_statistics(ids, *, on_done=None)`。v0.2.0 從模組層匯入，
`ImportError` 被吞掉、回傳 `False`，於是「清除帳本」永遠留下兩條對不上的外部統計。

### 5. 手動模式結算後度數會消失（新測試抓到的第五個錯）

`async_confirm_close` 只把 `proposal["actual_kwh"]`（電費單上的度數）寫進紀錄，但手動模式
填的是「本期度數」那個 number 欄位；而 `_write_reading` 一寫完就把 `manual_kwh` 歸零。
結果：手動模式結算完那期是**空的**，上期／上上期永遠空白，而且 `_statistic_rows` 會跳過
沒有度數的期別，外部統計一條都寫不出來。現在手動模式會退回使用 `proposal["kwh"]`。

### 測試

- 新增 `tests/ha/`：**24 項測試跑在真的 Home Assistant 2026.10.1 上**（
  `pytest-homeassistant-custom-component==0.13.372`），涵蓋設定流程（新增／重複戶別擋下／
  欄位驗證／重新設定／選項）、真的 config entry 設定、entity registry、**活的 recorder
  資料庫**（讀取與寫入外部統計）、九個服務的完整流程，以及 v1→v2 的 store 遷移。
  跑法見 README，或 `pip install -r requirements_test_ha.txt && pytest`。
- 這套測試會自己把 repo 的 `custom_components/taipower_bill` 複製進拋棄式的 HA 設定目錄，
  **不需要**任何指向 site-packages 的 symlink，也永遠測到工作目錄裡的程式碼。
- 離線套件（`tests/test_billing.py`、`tests/test_coordinator.py`）從 212 項擴充到 **218 項**，
  並修掉三處「stub 比真貨寬鬆」的地方：`Store` 的簽章、`Recorder.async_clear_statistics`
  的位置、以及 `HomeAssistant.loop`。
- 上面四個缺陷都**逐一重新植入**驗證過：把它們改回壞的版本，新測試確實會紅（真機套件
  四個全抓到；離線套件抓得到其中三個，服務參數個數那一個只有真機套件看得到）。

### 升級

覆蓋檔案、重啟即可。**如果你裝的是 v0.2.0，它從來沒有真的跑起來過**（不會有任何 entity），
所以這個升級是從「沒有東西」變成「有東西」；若你仍是 v0.1.x，資料會自動遷移。

## 0.2.0 — 2026-10-14

把整個帳期模型換掉：**結算不再是一個「事件」，而是一份可以事後修改的清單。**

起因是一句回報：「如果不是當天按，或者不小心提前按，就很難做調整，而且要如何回看前幾個月
的資料。」v0.1 的結算會把 `last_read_date` 往後推一個 `間隔天數` 並無法撤銷，所以按晚一天、
按早一天、按錯一次，帳期就跟著歪掉，而歷史只能靠單向累積。

### 帳期模型：歷次抄見日清單

- 一期 = **（上一筆抄見日的隔天）→（這一筆抄見日）**。所有視窗由清單推導，不再由「按下
  按鈕的瞬間」決定。
- **未來日期是合法的。** 電費單上的「下次抄表日」直接寫進清單，它就取代 `間隔天數` 的推定值
  ——這是「回看」與「預先知道帳期」的關鍵。
- **清單為準，按鈕只是快捷。** 新增 `add_reading`（原樣寫入）、`update_reading`（改日期／
  度數／金額並重算）、`undo_close_period`（撤銷最後一期）、`period_report`（任意期間回算）。
- 儲存格式 v1（`periods`）→ v2（`readings`）。載入時自動遷移：v0.1 的 `periods` 與
  `options.last_read_date` 都會被折進清單，**舊歷史不會消失**。遷移邏輯寫在 `TaipowerLedger`
  裡，所以可以離線單元測試。

### 防誤按：先提出、再確認

HA 的服務沒有內建確認步驟（`tap_action: confirmation:` 只管 Lovelace），所以狀態機自己管：

- 「結算本期」只**提出**：寫入 `pending_close`、跳 persistent notification、讓
  `confirm`／`cancel` 兩顆按鈕變可用；**此時完全不寫入**（連 store 都不存）。
- 15 分鐘內確認才寫入；逾時自動失效，無需清理。
- 新增三顆按鈕：`close_period`（提出）／`confirm_close`（寫入）／`cancel_close`（丟掉）。
  後兩顆在沒有待確認結算時是 `unavailable`。
- 提前結算會被擋下並說明原因（要 `force: true` 才能硬過，並在紀錄上留下 `forced` 標記）。

### 回看歷史：四條路

1. **HA 長期統計**：每期用 `async_add_external_statistics` 寫成
   `taipower_bill:<戶別>_period_kwh` / `_period_cost`。同一 `start` 是 update 而非 append，
   所以改寫某期就是整條重寫，不會出現兩條線。
2. **感測器**：新增 `last_period_kwh` / `last_period_cost` / `last_period_avg_price` /
   `prev_period_kwh` / `prev_period_cost` / `pending_close_date`（共 17 顆感測器）。
3. **`period_report` 服務**：任意期間回算明細，不必先結算過。
4. **卡片**：歷史表可展開全部期別。

### 其他修正

- **手動模式的結算時序**：`close_period` 原本只讀上一次輪詢的快照，所以「剛填完度數就按
  結算」會以「沒有可用的本期度數」被拒。現在手動模式直接讀 `manual_kwh`，不必等下一次輪詢。
- 設定問題改用清單語意：`no_readings`（空清單）與 `no_current_reading`（清單全在未來）
  分開，訊息直接指出怎麼修。
- 診斷（diagnostics）改為輸出抄見日清單、待確認結算、每期快取與外部統計 id。
- 測試從 69 項擴充到 **212 項**：帳期算術、提出→確認→撤銷→改寫、recorder 呼叫參數、
  外部統計 metadata 與列的時間戳、v1→v2 遷移、17 顆感測器的合法性與屬性契約。

### 升級

覆蓋檔案後重啟即可，舊資料會自動遷移。建議升級後到「統計」頁確認兩條
`taipower_bill:*` 外部統計已經出現（若 v0.1 從未成功寫入過統計，第一次會由背景的
`async_prepare` 補寫）。

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
