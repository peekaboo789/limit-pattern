import SwiftUI
import Observation

@MainActor
@Observable
final class AppState {
    enum Phase { case checking, login, ready }
    var phase: Phase = .checking
    var serverText: String = UserDefaults.standard.string(forKey: "server") ?? ""
    var username: String = UserDefaults.standard.string(forKey: "username") ?? ""
    var health: JSON = .null
    var authStatus: JSON = .null
    var loginError: String?
    var busy = false

    let api = APIClient.shared

    var serverVersion: String { health["version"].string ?? "" }
    var versionWarning: String? {
        guard !serverVersion.isEmpty else { return nil }
        return APIClient.compareVersion(serverVersion, APIClient.minServerVersion) < 0
            ? "服务器版本 \(serverVersion) 太旧，App 需要 \(APIClient.minServerVersion) 以上，部分功能不能用。请先升级 Docker 镜像。" : nil
    }
    /// 0.5.1 起用账号 + 密码。旧服务器只要密码。
    var needsUsername: Bool {
        if authStatus["username_required"].bool || authStatus["has_users"].bool || !authStatus["username"].isNull { return true }
        return APIClient.compareVersion(serverVersion.isEmpty ? "0.5.1" : serverVersion, "0.5.1") >= 0
    }

    init() {
        api.onUnauthorized = { [weak self] in
            Task { @MainActor in
                guard let self, self.phase == .ready else { return }
                self.loginError = "登录已失效（可能改了密码），请重新登录"
                self.phase = .login
            }
        }
    }

    func bootstrap() async {
        guard let url = APIClient.normalize(serverText) else { phase = .login; return }
        api.baseURL = url
        api.restoreCookieIfNeeded()
        do {
            health = try await api.get("/api/health", timeout: 12)
            authStatus = (try? await api.get("/api/auth/status", timeout: 12)) ?? .null
            if authStatus.isNull || !authStatus["required"].bool || authStatus["logged_in"].bool {
                // 旧服务器没有 auth/status：试一个受保护接口
                _ = try await api.get("/api/settings", timeout: 12)
                api.backupCookie()
                phase = .ready
            } else { phase = .login }
        } catch {
            if loginError == nil { loginError = (error as? APIError)?.isAuth == true ? nil : error.localizedDescription }
            phase = .login
        }
    }

    func connect(password: String) async {
        loginError = nil
        guard let url = APIClient.normalize(serverText) else { loginError = "服务器地址不对，例如 http://192.168.1.10:8000"; return }
        busy = true; defer { busy = false }
        api.baseURL = url
        UserDefaults.standard.set(url.absoluteString, forKey: "server")
        UserDefaults.standard.set(username, forKey: "username")
        serverText = url.absoluteString
        do {
            health = try await api.get("/api/health", timeout: 15)
            authStatus = (try? await api.get("/api/auth/status", timeout: 15)) ?? .null
            if authStatus["required"].bool && !authStatus["logged_in"].bool || !password.isEmpty {
                try await api.login(username: username, password: password)
            }
            _ = try await api.get("/api/settings", timeout: 15)
            api.backupCookie()
            phase = .ready
        } catch let e as APIError where e.isAuth {
            loginError = e.message == "HTTP 401" ? "账号或密码不对" : e.message
        } catch {
            loginError = error.localizedDescription
        }
    }

    func logout() async {
        _ = try? await api.post("/api/auth/logout")
        api.clearSession()
        phase = .login
    }

    func refreshHealth() async { if let h = try? await api.get("/api/health", timeout: 15) { health = h } }
}
