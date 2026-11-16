# 生成與恢復

在作品旁保存 generation-ledger.json；此為代理工作紀錄，不取代 CLI 管理的 project.json。每項任務包含 attemptId、tool、startedAt、identityReferences、styleReferences、prompt、expectedSlots、expectedCaptions、status、returnedAssetPath、error。狀態使用 planned、dispatched、awaiting_asset、acquired、failed；檔案真的可解碼才 acquired。原始回傳路徑、匯入來源 hash 與 slot 對應保留以便去重。中斷或回傳不明先檢查紀錄與已存在素材，不盲目重派。

提示詞需包含：角色不可變特徵、身分參考、獨立畫風參考、每格 slot／逐字繁中／動作／用途、字體和場景整體設計、透明底、完整邊緣與安全留白、無跨格內容。布局依實際工具尺寸決定。未指定時 8 張是邏輯批次；使用者指定「8 張一畫布」時，將它記錄為本作品的 canvasMode，固定 01–08、09–16 等 slot 對應與格序，後續修圖沿用畫布。這是作品選擇，不是所有作品的強制規則。合圖減少生成次數，不代表單次成本、額度或總算力與一張貼圖相同。

研究其他 IP 的表情時，先將原角色的眼白／瞳孔／眼框結構列入身分約束，再用嘴角、眼瞼、頭部角度和肢體表達新態度。研究參考提供聊天用途與表現原理，不取代身分參考的五官結構。生成後依 visual-qa 的 `eyeStyle` 欄位核對適用角色。

每格計畫除了台詞與動作，補上可直接替換的聊天情境：對誰、收到什麼或發生什麼、這張替發送者說什麼、預期對方如何接話。套組可有故事進展，但每張要能獨立使用。參考別作的對話用途和構圖原理，保留自己的角色與原創台詞；不能把收藏或愛心數當銷量。

畫布任務另記 canvasId、grid（欄列與像素尺寸）、slotMap、sourceAttemptId、repairScope、preserveScope、qaReportPath。生成後依 [畫布檢驗與局部修正](visual-qa.md) 檢查；不在 ledger 中以 acquired 代表驗收通過。若工具仍會改動其餘格，保存新版本、比較全部格並重新驗收，不能假定局部修正就維持像素不變。

使用當次工具實際支援的參考圖參數。只有聊天圖片且工具支援最近圖片引用時，可用該機制；本機圖片先查看。工具未提供模型選項就不承諾指定模型。沒有生成工具時可以先完成劇本與保存進度，明確指出待取得原圖。

文字策略：integrated 產圖直接含正確台詞、匯入後停用疊字；editable 產無字圖、加工器用字型配字；none 不產字、不疊字。不要為了可編輯承諾將含字來源自動拆成字與圖層。
