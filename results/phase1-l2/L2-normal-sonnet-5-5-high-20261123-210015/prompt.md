你在一台 Ubuntu 22.04 的機器上，上面已經有一個單節點的 K3s 叢集（`kubectl`、`helm` 都可以直接用，需要 root 時可以用 `sudo`）。這台機器的 kernel 已經載入 gtp5g 模組。

叢集裡已經有一個 MEC（邊緣運算）應用：namespace `mec` 的 `mec-app`，IP `10.43.200.10`，HTTP port 80。不要修改它。

請在這個叢集上用 free5GC 部署一個 5G SA 網路，並用 UERANSIM 模擬基地台與手機。使用者面要用 ULCL（Uplink Classifier）把 MEC 的流量分出去：

- 1 個 gNB、1 個 UE
- 3 個 UPF：1 個 I-UPF（ULCL／branching UPF，接 gNB）、2 個 PSA-UPF
  - UE 送往 MEC（`10.43.200.10`）的流量：I-UPF → **MEC 專用的 PSA-UPF** → MEC
  - 其他流量（上網）：I-UPF → **另一個 PSA-UPF** → 網際網路
- PLMN：MCC 208、MNC 93
- 1 個 slice：SST 1、SD 010203
- 1 個 DNN：`internet`
- UE 的 SUPI：`imsi-208930000000001`
- 版本：free5GC **v4.3.0**。Helm chart 請用 https://github.com/free5gc/free5gc-helm 的 commit `07139fcc6f7579928a0722e274bdc876a4a4e240`（free5GC v4.3.0 對應的版本），不要用其他版本或 `main` 的最新版

完成條件：UE 要成功註冊並建立 PDU session，而且透過 5G 網路（UE 的 tunnel 介面）：(1) 能連到網際網路，例如 ping 8.8.8.8；(2) 能連到 MEC 應用（`http://10.43.200.10/`），而且往 MEC 的流量確實經過 MEC 專用的 PSA-UPF，上網的流量則不經過它。

這是無人值守的工作：不會有人回答你的問題，請自己判斷、自己完成。

完成後請說明：(1) 你部署了哪些元件、用了哪些設定；(2) 你怎麼確認這個網路是正確的，特別是怎麼確認兩種流量各自走了哪個 UPF。
