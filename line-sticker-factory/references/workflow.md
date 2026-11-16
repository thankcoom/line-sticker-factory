# 加工與檔案契約

Python 3.10+；依賴見技能根目錄 requirements.txt。先確認目前環境是否可用；缺依賴才在隔離環境以 `python3 -m pip install -r "$SKILL_ROOT/requirements.txt"` 安裝。不要自動改使用者的全域環境。以下 SKILL_ROOT 是本技能絕對路徑，PROJECT 是作品絕對路徑；shell 變數須先設定。

在作品旁記錄 Python 絕對路徑、版本及 Pillow 版本，後續 render/build/validate 使用同一環境。某些 shell 依工作目錄切換 Python；不能只因指令都叫 python3 就認定環境一致。主圖與 tab 在 package_images 中由來源重新縮放，Pillow 版本不同可能改變輸出 hash。遇到支援圖 hash mismatch，先比對來源與製作環境，以原環境重驗；不要修改 manifest/hash 來放行。換環境重建則另建版本並重做視覺驗收，保留原交付 ZIP 與 PNG；備份可還原原圖不代表任意 Pillow 版本可重建逐位元相同輸出。

```sh
python3 "$SKILL_ROOT/scripts/sticker_project.py" --help
python3 "$SKILL_ROOT/scripts/sticker_project.py" init --project "$PROJECT" --count 8 --name 我的貼圖
python3 "$SKILL_ROOT/scripts/sticker_project.py" plan --project "$PROJECT" --file /absolute/production-plan.json
python3 "$SKILL_ROOT/scripts/sticker_project.py" status --project "$PROJECT"
```

production-plan.json 為下列物件；slots 必須包含每一個 ordinal 恰好一次，不能只有範例中的一項。count 合法值為 8、16、24、32、40。brief.textStrategy 為 integrated、editable 或 none。未指定的舊作品維持 editable。

```json
{"schemaVersion":1,"brief":{"name":"我的貼圖","count":8,"language":"zh-Hant","textStrategy":"integrated"},"slots":[{"ordinal":1,"caption":"謝謝你","action":"雙手捧花鞠躬，字體沿花束弧線排列","useCase":"表達感謝"}]}
```

含字來源匯入示例（修改含字圖的台詞時也是這條路徑）：

```sh
python3 "$SKILL_ROOT/scripts/sticker_project.py" import --project "$PROJECT" --slot 3 --image /absolute/new-source.png --image-contains-caption --expected-caption '謝謝你'
python3 "$SKILL_ROOT/scripts/sticker_project.py" render --project "$PROJECT" --slot 3
python3 "$SKILL_ROOT/scripts/sticker_project.py" preview --project "$PROJECT" --out /absolute/preview.png
```

多格來源加 `--crop x,y,width,height`，座標基於 EXIF 校正後的實際像素。匯入保留原來源；不要覆寫舊檔。同一張已含字來源換 crop 或取消旗標不能解鎖配字。已鎖定圖片不要先用 caption 或 plan 改字；以新圖片和 expected-caption 更新其預期台詞。

可編輯來源不加含字旗標；配字使用已存在且有授權使用的字型：

```sh
python3 "$SKILL_ROOT/scripts/sticker_project.py" caption --project "$PROJECT" --slot 3 --text '謝謝你'
python3 "$SKILL_ROOT/scripts/sticker_project.py" render --project "$PROJECT" --slot 3 --font /absolute/font.ttf
```

查看圖片後才執行 review，不把下面命令當作自動核准：

```sh
python3 "$SKILL_ROOT/scripts/sticker_project.py" review --project "$PROJECT" --slot 3 --status pass --reviewer agent --reason '已查看最終尺寸，逐字核對與透明邊緣通過'
python3 "$SKILL_ROOT/scripts/sticker_project.py" build --project "$PROJECT" --out /absolute/exports
python3 "$SKILL_ROOT/scripts/sticker_project.py" validate --project "$PROJECT" --zip /absolute/version/line-upload.zip
python3 "$SKILL_ROOT/scripts/sticker_project.py" restore --archive /absolute/version/project-backup.zip --project /absolute/restored
```

build JSON 回傳真實輸出路徑；依回傳值交付，勿自行拼接版本目錄。失效後重新 render、查看、review、build。restore 目的地須新建／無衝突，缺原字型只能保留 PNG，不能聲稱可重新排字。

processor 內建檢查 PNG、透明區、像素／位元組上限、尺寸、hash 與目前 review。主圖／tab 也要看；含字來源的 base 不會神奇移除文字，若縮小後不可讀應提供合適來源並用工具支援的正式途徑處理，不直接改 manifest。需要新參數先讀該子命令 --help。
