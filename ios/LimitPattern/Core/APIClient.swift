import Foundation

struct APIError: LocalizedError {
    let status: Int
    let message: String
    var errorDescription: String? { message }
    var isAuth: Bool { status == 401 }
}

/// 和 Docker 版的 /api 通讯。会话靠 HttpOnly cookie `lp_session`（HTTPCookieStorage 持久保存），
/// 同时把 cookie 值备份到钥匙串，系统清掉 cookie 时自动补回。
final class APIClient: @unchecked Sendable {
    static let cookieName = "lp_session"
    static let minServerVersion = "0.5.2"
    static let shared = APIClient()

    var baseURL: URL?
    var onUnauthorized: (() -> Void)?
    let session: URLSession

    init(storage: HTTPCookieStorage = .shared) {
        let cfg = URLSessionConfiguration.default
        cfg.httpCookieStorage = storage
        cfg.httpCookieAcceptPolicy = .always
        cfg.httpShouldSetCookies = true
        cfg.timeoutIntervalForRequest = 60
        cfg.timeoutIntervalForResource = 900
        session = URLSession(configuration: cfg)
    }

    static func normalize(_ raw: String) -> URL? {
        var s = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        if s.isEmpty { return nil }
        if !s.lowercased().hasPrefix("http://") && !s.lowercased().hasPrefix("https://") { s = "http://" + s }
        while s.hasSuffix("/") { s.removeLast() }
        if s.hasSuffix("/api") { s.removeLast(4) }
        guard let u = URL(string: s), u.host != nil else { return nil }
        return u
    }

    func url(_ path: String, query: [String: String?] = [:]) throws -> URL {
        guard let baseURL else { throw APIError(status: 0, message: "还没有填写服务器地址") }
        var comps = URLComponents(url: baseURL.appendingPathComponent(path.hasPrefix("/") ? String(path.dropFirst()) : path), resolvingAgainstBaseURL: false)!
        let items = query.compactMap { k, v in v.map { URLQueryItem(name: k, value: $0) } }
        if !items.isEmpty { comps.queryItems = items.sorted { $0.name < $1.name } }
        return comps.url!
    }

    // MARK: cookie
    var storage: HTTPCookieStorage { session.configuration.httpCookieStorage ?? .shared }

    func sessionCookie() -> HTTPCookie? {
        guard let baseURL else { return nil }
        return storage.cookies(for: baseURL)?.first { $0.name == Self.cookieName }
    }

    func backupCookie() {
        guard let c = sessionCookie() else { return }
        Keychain.set(c.value, for: "cookie:" + (baseURL?.absoluteString ?? ""))
    }

    /// 系统 cookie 丢了就用钥匙串里的备份补一个（有效期一年，服务器会按签名判断是否失效）。
    @discardableResult
    func restoreCookieIfNeeded() -> Bool {
        guard let baseURL, sessionCookie() == nil, let v = Keychain.get("cookie:" + baseURL.absoluteString), !v.isEmpty else { return false }
        if let c = Self.makeCookie(value: v, for: baseURL) { storage.setCookie(c); return true }
        return false
    }

    static func makeCookie(value: String, for url: URL) -> HTTPCookie? {
        HTTPCookie(properties: [.name: cookieName, .value: value, .domain: url.host ?? "", .path: "/",
                                .expires: Date().addingTimeInterval(365 * 86400)])
    }

    func clearSession() {
        if let baseURL {
            storage.cookies(for: baseURL)?.forEach { storage.deleteCookie($0) }
            Keychain.set(nil, for: "cookie:" + baseURL.absoluteString)
        }
    }

    // MARK: requests
    func raw(_ method: String, _ path: String, query: [String: String?] = [:], body: Any? = nil, timeout: TimeInterval = 60) async throws -> (Data, HTTPURLResponse) {
        var req = URLRequest(url: try url(path, query: query))
        req.httpMethod = method
        req.timeoutInterval = timeout
        req.setValue("application/json", forHTTPHeaderField: "Accept")
        if let body {
            req.setValue("application/json; charset=utf-8", forHTTPHeaderField: "Content-Type")
            req.httpBody = try JSONSerialization.data(withJSONObject: body)
        }
        let data: Data, resp: URLResponse
        do { (data, resp) = try await session.data(for: req) }
        catch let e as URLError {
            let msg: String
            switch e.code {
            case .timedOut: msg = "请求超时，服务器可能正在计算，稍后再试"
            case .cannotConnectToHost, .cannotFindHost: msg = "连不上服务器，检查地址、端口和网络"
            case .notConnectedToInternet: msg = "手机没有联网"
            case .secureConnectionFailed, .serverCertificateUntrusted: msg = "HTTPS 证书有问题：\(e.localizedDescription)"
            default: msg = e.localizedDescription
            }
            throw APIError(status: 0, message: msg)
        }
        guard let http = resp as? HTTPURLResponse else { throw APIError(status: 0, message: "服务器没有返回") }
        if http.statusCode == 401 && !path.contains("auth/login") {
            await MainActor.run { self.onUnauthorized?() }
        }
        if !(200..<300).contains(http.statusCode) {
            throw APIError(status: http.statusCode, message: Self.detail(data) ?? "HTTP \(http.statusCode)")
        }
        return (data, http)
    }

    static func detail(_ data: Data) -> String? {
        guard let j = try? JSON.parse(data) else { return String(data: data, encoding: .utf8).flatMap { $0.isEmpty ? nil : String($0.prefix(300)) } }
        let d = j["detail"]
        if let s = d.string { return s }
        if let first = d.array.first { return first["msg"].string ?? "请求参数不对" }
        return j["message"].string
    }

    func json(_ method: String, _ path: String, query: [String: String?] = [:], body: Any? = nil, timeout: TimeInterval = 60) async throws -> JSON {
        let (data, _) = try await raw(method, path, query: query, body: body, timeout: timeout)
        if data.isEmpty { return .null }
        return try JSON.parse(data)
    }
    func get(_ path: String, _ query: [String: String?] = [:], timeout: TimeInterval = 60) async throws -> JSON { try await json("GET", path, query: query, timeout: timeout) }
    func post(_ path: String, _ body: Any? = [String: Any](), timeout: TimeInterval = 60) async throws -> JSON { try await json("POST", path, body: body, timeout: timeout) }

    /// 下载导出文件存到临时目录，返回可分享的文件 URL。
    func download(_ path: String, query: [String: String?] = [:], fallbackName: String) async throws -> URL {
        let (data, http) = try await raw("GET", path, query: query, timeout: 300)
        let name = Self.filename(from: http.value(forHTTPHeaderField: "Content-Disposition")) ?? fallbackName
        let dir = FileManager.default.temporaryDirectory.appendingPathComponent("exports", isDirectory: true)
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        let url = dir.appendingPathComponent(name.replacingOccurrences(of: "/", with: "_"))
        try data.write(to: url, options: .atomic)
        return url
    }

    static func filename(from disposition: String?) -> String? {
        guard let d = disposition else { return nil }
        if let r = d.range(of: "filename*=UTF-8''") {
            let rest = d[r.upperBound...].split(separator: ";").first.map(String.init) ?? ""
            return rest.removingPercentEncoding
        }
        if let r = d.range(of: "filename=") {
            return d[r.upperBound...].split(separator: ";").first.map { $0.trimmingCharacters(in: CharacterSet(charactersIn: "\" ")) }
        }
        return nil
    }

    // MARK: auth
    /// 0.5.1：{username, password, remember}（记住我 = 长期 cookie）；0.5.0 只认 password，多传字段不影响。
    func login(username: String, password: String) async throws {
        var body: [String: Any] = ["password": password, "remember": true]
        if !username.isEmpty { body["username"] = username }
        _ = try await post("/api/auth/login", body)
        backupCookie()
    }

    static func compareVersion(_ a: String, _ b: String) -> Int {
        let pa = a.split(whereSeparator: { !$0.isNumber }).compactMap { Int($0) }
        let pb = b.split(whereSeparator: { !$0.isNumber }).compactMap { Int($0) }
        for i in 0..<max(pa.count, pb.count) {
            let x = i < pa.count ? pa[i] : 0, y = i < pb.count ? pb[i] : 0
            if x != y { return x < y ? -1 : 1 }
        }
        return 0
    }
}
