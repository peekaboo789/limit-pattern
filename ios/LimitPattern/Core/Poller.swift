import Foundation

/// 后台监控轮询：App 在前台或刚切后台时，拿 /api/monitor/events 与自选股监控发本地通知（只在交易时段，服务器已按时段过滤）。
@MainActor
final class AlertPoller {
    static let shared = AlertPoller()
    private var task: Task<Void, Never>?
    private var lastId: Int = UserDefaults.standard.integer(forKey: "events_last_id")
    private var lastDate: String = UserDefaults.standard.string(forKey: "events_date") ?? ""
    private var seenWatch: Set<String> = []

    func start() {
        guard task == nil else { return }
        task = Task { [weak self] in
            while !Task.isCancelled {
                await self?.tick()
                try? await Task.sleep(for: .seconds(15))
            }
        }
    }
    func stop() { task?.cancel(); task = nil }

    func tick() async {
        let api = APIClient.shared
        guard api.baseURL != nil, UserDefaults.standard.object(forKey: "local_alerts") as? Bool ?? true else { return }
        if let ev = try? await api.get("/api/monitor/events", ["since": String(lastId)], timeout: 15) {
            let date = ev["date"].string ?? ""
            if date != lastDate { lastDate = date }
            for e in ev["events"].array.isEmpty ? ev["items"].array : ev["events"].array {
                let name = e["name"].string ?? e["code"].text
                let pct = e["pct"].double
                Notifier.post(title: (e["level"].string == "alert" ? "🔴 " : "🟠 ") + "\(name) \(Theme.pctText(pct))",
                              body: "AI 榜单 · \(e["code"].text) · \(e["message"].string ?? "涨幅达到报警线")", id: "ev-\(e["id"].text)")
            }
            if let id = ev["last_id"].int { lastId = id }
            UserDefaults.standard.set(lastId, forKey: "events_last_id")
            UserDefaults.standard.set(lastDate, forKey: "events_date")
        }
        if let wl = try? await api.get("/api/watchlist/monitor", timeout: 15), wl["alerts_active"].bool, wl["alerts_enabled"].bool || wl["alerts_enabled"].isNull {
            let day = wl["date"].string ?? DateFormatter.ymd.string(from: Date())
            for it in wl["items"].array {
                let lv = it["level"].string ?? "normal"
                guard lv == "warn" || lv == "alert" else { continue }
                let key = "\(day)-\(it["code"].text)-\(lv)"
                if seenWatch.contains(key) { continue }
                seenWatch.insert(key)
                Notifier.post(title: (lv == "alert" ? "🔴 " : "🟠 ") + "自选 \(it["name"].text) \(Theme.pctText(it["pct"].double))",
                              body: "现价 \(it["price"].fmt()) · \(it["code"].text)", id: "wl-\(key)")
            }
        }
    }
}

extension DateFormatter {
    static let ymd: DateFormatter = { let f = DateFormatter(); f.dateFormat = "yyyy-MM-dd"; f.timeZone = TimeZone(identifier: "Asia/Shanghai"); return f }()
}
