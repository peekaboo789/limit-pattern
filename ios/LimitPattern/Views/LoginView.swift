import SwiftUI

struct LoginView: View {
    @Environment(AppState.self) private var state
    @State private var password = ""
    @FocusState private var focus: Int?

    var body: some View {
        @Bindable var state = state
        ScrollView {
            VStack(spacing: 22) {
                Image(systemName: "chart.bar.fill").font(.system(size: 54)).foregroundStyle(Theme.gold)
                    .padding(26).glassEffect(.regular.tint(Theme.gold.opacity(0.2)), in: .circle)
                    .padding(.top, 50)
                Text("涨停形态").font(.largeTitle.bold())
                Text("连接你的 Docker 版服务器").foregroundStyle(.secondary)

                GlassEffectContainer(spacing: 14) {
                    VStack(spacing: 12) {
                        field("服务器地址", systemImage: "server.rack") {
                            TextField("http://192.168.1.10:8000", text: $state.serverText)
                                .keyboardType(.URL).textInputAutocapitalization(.never).autocorrectionDisabled().focused($focus, equals: 0)
                        }
                        field("账号", systemImage: "person") {
                            TextField(state.needsUsername ? "管理员账号" : "账号（旧版服务器可留空）", text: $state.username)
                                .textInputAutocapitalization(.never).autocorrectionDisabled().textContentType(.username).focused($focus, equals: 1)
                        }
                        field("密码", systemImage: "lock") {
                            SecureField("密码", text: $password).textContentType(.password).focused($focus, equals: 2)
                                .onSubmit { Task { await state.connect(password: password) } }
                        }
                    }
                }
                ErrorBanner(message: state.loginError)
                Button {
                    focus = nil
                    Task { await state.connect(password: password); if state.phase == .ready { password = "" } }
                } label: {
                    HStack { if state.busy { ProgressView() }; Text(state.busy ? "连接中…" : "登录").bold() }.frame(maxWidth: .infinity).padding(.vertical, 6)
                }
                .buttonStyle(.glassProminent).tint(Theme.gold).disabled(state.busy)

                VStack(alignment: .leading, spacing: 6) {
                    Text("· 登录一次后会记住，服务器改密码后才需要重新登录。")
                    Text("· 局域网可以用 http://IP:端口；外网建议用 HTTPS 反向代理。")
                    Text("· 服务器没设密码时，账号密码留空直接点登录。")
                }.font(.footnote).foregroundStyle(.secondary).frame(maxWidth: .infinity, alignment: .leading)
            }
            .padding(22).frame(maxWidth: 520)
            .frame(maxWidth: .infinity)
        }
        .scrollDismissesKeyboard(.interactively)
        .screenBackground()
    }

    func field<C: View>(_ title: String, systemImage: String, @ViewBuilder _ content: () -> C) -> some View {
        HStack(spacing: 12) {
            Image(systemName: systemImage).frame(width: 22).foregroundStyle(Theme.gold)
            content()
        }
        .padding(.horizontal, 16).padding(.vertical, 14)
        .glassEffect(.regular.interactive(), in: .rect(cornerRadius: 18))
        .accessibilityLabel(title)
    }
}
