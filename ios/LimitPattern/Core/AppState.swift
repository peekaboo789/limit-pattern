import SwiftUI
import Observation

struct SavedLogin: Identifiable {
    let server: Server
    let account: SavedAccount
    var id: String { account.accountKey }
    var username: String { account.username }
    var lastUsed: Date { account.lastUsed }
}

@MainActor
@Observable
final class AppState {
    enum Phase { case checking, login, ready }
    enum QuickLoginOutcome { case ready, needsPassword, failed }
    var phase: Phase = .checking
    var username: String = ""
    var health: JSON = .null
    var authStatus: JSON = .null
    var loginError: String?
    var busy = false
    var loggingInKey: String?
    var debugStayOnLogin = false

    var api = APIClient.shared
    let servers: ServerStore
    let accounts: AccountStore

    var serverText: String { servers.selected?.baseURL ?? "" }

    var serverVersion: String { health["version"].string ?? "" }
    var versionWarning: String? {
        guard !serverVersion.isEmpty else { return nil }
        return APIClient.compareVersion(serverVersion, APIClient.minServerVersion) < 0
            ? "服务器版本 \(serverVersion) 太旧，App 需要 \(APIClient.minServerVersion) 以上，部分功能不能用。请先升级 Docker 镜像。" : nil
    }
    var selectedServer: Server? { servers.selected }
    var accountsForSelectedServer: [SavedAccount] { accounts.accounts(forServerID: selectedServer?.id ?? "") }
    var savedLogins: [SavedLogin] {
        var seen: [String: SavedLogin] = [:]
        for account in accounts.accounts.sorted(by: { $0.lastUsed > $1.lastUsed }) {
            guard let server = servers.servers.first(where: { $0.id == account.serverID }) else { continue }
            let key = account.serverID + "+" + account.username
            if seen[key] == nil {
                seen[key] = SavedLogin(server: server, account: account)
            }
        }
        return seen.values.sorted { $0.lastUsed > $1.lastUsed }
    }
    var hasSavedLogins: Bool { !savedLogins.isEmpty }

    /// 0.5.1 起用账号 + 密码。旧服务器只要密码。
    var needsUsername: Bool {
        if authStatus["setup_needed"].bool || !(authStatus["username"].string ?? "").isEmpty { return true }
        return APIClient.compareVersion(serverVersion.isEmpty ? "0.5.1" : serverVersion, "0.5.1") >= 0
    }
    var setupNeeded: Bool { authStatus["setup_needed"].bool }

    init(servers: ServerStore? = nil, accounts: AccountStore? = nil, api: APIClient = APIClient.shared) {
        self.servers = servers ?? ServerStore()
        self.accounts = accounts ?? AccountStore()
        self.username = UserDefaults.standard.string(forKey: "username") ?? ""
        self.api = api
        migrateIfNeeded()
        seedSavedLoginsIfRequested()
        api.onUnauthorized = { [weak self] in
            Task { @MainActor in
                guard let self, self.phase == .ready else { return }
                self.loginError = "登录已失效（可能改了密码），请重新登录"
                self.phase = .login
            }
        }
    }

    func bootstrap() async {
        if debugStayOnLogin {
            phase = .login
            return
        }
        guard let server = selectedServer, let url = APIClient.normalize(server.baseURL) else {
            phase = .login
            return
        }
        selectServer(server, clearPrevious: false)
        api.restoreCookieIfNeeded()
        do {
            health = try await api.get("/api/health", timeout: 12)
            authStatus = (try? await api.get("/api/auth/status", timeout: 12)) ?? .null
            if authStatus.isNull || !authStatus["required"].bool || authStatus["logged_in"].bool {
                // 旧服务器没有 auth/status：试一个受保护接口
                _ = try await api.get("/api/settings", timeout: 12)
                api.backupCookie()
                phase = .ready
            } else {
                if let u = authStatus["username"].string, !u.isEmpty {
                    username = u
                }
                phase = .login
            }
        } catch {
            if loginError == nil { loginError = (error as? APIError)?.isAuth == true ? nil : error.localizedDescription }
            phase = .login
        }
    }

    func reconcileServer(_ server: Server, activate: Bool, previousURL: String?) {
        guard activate else { return }
        if previousURL != server.baseURL {
            if let previousURL, let old = APIClient.normalize(previousURL) {
                api.baseURL = old
                api.clearSession()
            }
            username = ""
            health = .null
            authStatus = .null
            loginError = nil
            phase = .login
        }
        api.baseURL = APIClient.normalize(server.baseURL)
    }

    func removeServer(_ id: String) {
        let wasSelected = selectedServer?.id == id
        if wasSelected { api.clearSession() }
        accounts.removeAll(forServerID: id)
        servers.remove(id: id)
        if wasSelected {
            if let next = selectedServer {
                api.baseURL = APIClient.normalize(next.baseURL)
            } else {
                api.baseURL = nil
            }
            username = ""
            health = .null
            authStatus = .null
            loginError = nil
            phase = .login
        }
    }

    func selectServer(_ server: Server, clearPrevious: Bool = true) {
        if clearPrevious {
            api.clearSession()
            phase = .login
        } else {
            phase = .checking
        }
        servers.select(id: server.id)
        api.baseURL = APIClient.normalize(server.baseURL)
        username = ""
        health = .null
        authStatus = .null
        loginError = nil
    }

    func connect(password: String) async {
        loginError = nil
        guard let server = selectedServer, let url = APIClient.normalize(server.baseURL) else {
            loginError = "服务器地址不对，例如 http://192.168.1.10:8000"
            return
        }
        busy = true; defer { busy = false }
        api.baseURL = url
        do {
            health = try await api.get("/api/health", timeout: 15)
            authStatus = (try? await api.get("/api/auth/status", timeout: 15)) ?? .null
            if authStatus["setup_needed"].bool {
                guard !username.isEmpty, !password.isEmpty else {
                    loginError = "首次使用：请填写要创建的管理员账号和密码"
                    return
                }
                _ = try await api.post("/api/auth/setup", ["username": username, "password": password])
            }
            if authStatus["required"].bool && !authStatus["logged_in"].bool || !password.isEmpty {
                try await api.login(username: username, password: password)
            }
            _ = try await api.get("/api/settings", timeout: 15)
            api.backupCookie()
            accounts.save(serverID: server.id, username: username, password: password, cookie: api.sessionCookie()?.value)
            phase = .ready
        } catch let e as APIError where e.isAuth {
            loginError = e.message == "HTTP 401" ? "账号或密码不对" : e.message
        } catch {
            loginError = error.localizedDescription
        }
    }

    @discardableResult
    func quickLogin(_ account: SavedAccount, password: String? = nil) async -> QuickLoginOutcome {
        loginError = nil
        guard let server = servers.servers.first(where: { $0.id == account.serverID }),
              let url = APIClient.normalize(server.baseURL) else {
            loginError = "已保存的服务器已不存在，请先选择服务器"
            return .failed
        }
        loggingInKey = account.accountKey
        defer { loggingInKey = nil }
        busy = true; defer { busy = false }
        selectServer(server, clearPrevious: true)
        username = account.username
        api.baseURL = url

        let cookie = password == nil ? accounts.cookie(forServerID: server.id, username: account.username) : nil
        if let cookie, !cookie.isEmpty {
            api.injectCookie(value: cookie, username: account.username)
        }

        do {
            health = try await api.get("/api/health", timeout: 15)
            authStatus = (try? await api.get("/api/auth/status", timeout: 15)) ?? .null
            if authStatus["logged_in"].bool {
                accounts.touch(serverID: server.id, username: account.username)
                accounts.updateCookie(serverID: server.id, username: account.username, cookie: api.sessionCookie()?.value)
                phase = .ready
                return .ready
            }

            let resolvedPassword = password ?? accounts.password(forServerID: server.id, username: account.username)
            if let resolvedPassword {
                try await api.login(username: account.username, password: resolvedPassword)
                _ = try await api.get("/api/settings", timeout: 15)
                api.backupCookie()
                accounts.save(serverID: server.id, username: account.username, password: resolvedPassword, cookie: api.sessionCookie()?.value)
                phase = .ready
                return .ready
            }

            loginError = "会话已过期，请重新输入密码"
            phase = .login
            return .needsPassword
        } catch let e as APIError where e.isAuth {
            loginError = e.message == "HTTP 401" ? "账号或密码不对" : e.message
        } catch {
            loginError = error.localizedDescription
        }
        return .failed
    }

    func logout() async {
        _ = try? await api.post("/api/auth/logout")
        api.clearSession()
        phase = .login
    }

    func logoutAll() async {
        _ = try? await api.post("/api/auth/logout-all")
        api.clearSession()
        phase = .login
    }

    private func migrateIfNeeded() {
        let migratedKey = "limitpattern.migratedToServers.v1"
        let defaults = servers.defaults
        guard !defaults.bool(forKey: migratedKey) else { return }

        let oldServer = defaults.string(forKey: "server") ?? ""
        let oldUsername = defaults.string(forKey: "username") ?? ""

        if !oldServer.isEmpty {
            let normalized = ServerURL.normalize(oldServer) ?? oldServer
            let server = (try? servers.upsert(name: normalized, baseURL: normalized)) ?? servers.servers.first
            if let server, !oldUsername.isEmpty {
                let cookie = accounts.keychain.get("cookie:" + normalized)
                    ?? accounts.keychain.get("cookie:" + oldUsername + ":" + (URL(string: normalized)?.host ?? ""))
                accounts.save(serverID: server.id, username: oldUsername, password: nil, cookie: cookie)
            }
        }

        defaults.set(true, forKey: migratedKey)
    }

    private func seedSavedLoginsIfRequested() {
        let defaults = UserDefaults.standard
        guard defaults.string(forKey: "savedLoginsSeed") == "1" else { return }
        let baseURL = defaults.string(forKey: "savedLoginsSeedURL") ?? "http://127.0.0.1:8000"
        let name = defaults.string(forKey: "savedLoginsSeedName") ?? "本地 Docker"
        let username = defaults.string(forKey: "savedLoginsSeedUser") ?? "admin"
        debugStayOnLogin = defaults.string(forKey: "savedLoginsSeedStayOnLogin") == "1"
        guard let server = try? servers.upsert(id: "screenshot-seed", name: name, baseURL: baseURL) else { return }
        accounts.save(serverID: server.id, username: username, password: nil, cookie: nil)
        defaults.removeObject(forKey: "savedLoginsSeed")
        defaults.removeObject(forKey: "savedLoginsSeedURL")
        defaults.removeObject(forKey: "savedLoginsSeedName")
        defaults.removeObject(forKey: "savedLoginsSeedUser")
        defaults.removeObject(forKey: "savedLoginsSeedStayOnLogin")
    }

    func refreshHealth() async { if let h = try? await api.get("/api/health", timeout: 15) { health = h } }
}
