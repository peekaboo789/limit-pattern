import SwiftUI

enum Theme {
    static let gold = Color(red: 0.91, green: 0.72, blue: 0.43)
    static let bg = Color(red: 0.063, green: 0.094, blue: 0.165)
    static let up = Color(red: 1.0, green: 0.36, blue: 0.45)     // A 股：红涨
    static let down = Color(red: 0.18, green: 0.86, blue: 0.60)  // 绿跌
    static let disclaimer = "相似度不是涨停概率，仅供复盘参考，不构成投资建议。"

    static func pctColor(_ v: Double?) -> Color {
        guard let v else { return .secondary }
        return v > 0 ? up : (v < 0 ? down : .secondary)
    }
    static func pctText(_ v: Double?) -> String {
        guard let v else { return "—" }
        return String(format: "%@%.2f%%", v > 0 ? "+" : "", v)
    }
}

struct Background: View {
    var body: some View {
        LinearGradient(colors: [Theme.bg, Color(red: 0.10, green: 0.08, blue: 0.16), Theme.bg], startPoint: .topLeading, endPoint: .bottomTrailing)
            .overlay(alignment: .topTrailing) { Circle().fill(Theme.gold.opacity(0.18)).frame(width: 260).blur(radius: 90).offset(x: 80, y: -60) }
            .overlay(alignment: .bottomLeading) { Circle().fill(Theme.up.opacity(0.10)).frame(width: 240).blur(radius: 90).offset(x: -60, y: 40) }
            .ignoresSafeArea()
    }
}

/// 液态玻璃卡片。
struct GlassCard<Content: View>: View {
    var tint: Color? = nil
    var alert = false
    @ViewBuilder var content: Content
    var body: some View {
        VStack(alignment: .leading, spacing: 10) { content }
            .padding(14)
            .frame(maxWidth: .infinity, alignment: .leading)
            .glassEffect(tint.map { Glass.regular.tint($0.opacity(0.25)) } ?? .regular, in: .rect(cornerRadius: 22))
            .overlay {
                if alert { RoundedRectangle(cornerRadius: 22).stroke(Theme.up, lineWidth: 2.5) }
            }
    }
}

struct PctText: View {
    let value: Double?
    var font: Font = .body.monospacedDigit().weight(.semibold)
    var body: some View { Text(Theme.pctText(value)).font(font).foregroundStyle(Theme.pctColor(value)) }
}

struct Disclaimer: View {
    var text: String = Theme.disclaimer
    var body: some View {
        Label(text, systemImage: "exclamationmark.triangle")
            .font(.caption).foregroundStyle(.secondary)
            .padding(.horizontal, 12).padding(.vertical, 8)
            .glassEffect(.regular, in: .capsule)
    }
}

struct ErrorBanner: View {
    let message: String?
    var body: some View {
        if let message, !message.isEmpty {
            Label(message, systemImage: "xmark.octagon").font(.callout).foregroundStyle(.white)
                .padding(12).frame(maxWidth: .infinity, alignment: .leading)
                .glassEffect(.regular.tint(.red.opacity(0.35)), in: .rect(cornerRadius: 16))
        }
    }
}

struct LevelBadge: View {
    let level: String
    var body: some View {
        if !level.isEmpty {
            Text(level).font(.caption2.weight(.bold)).padding(.horizontal, 8).padding(.vertical, 3)
                .foregroundStyle(level.contains("强") ? Theme.up : (level.contains("高") ? Theme.gold : .secondary))
                .glassEffect(.regular, in: .capsule)
        }
    }
}

extension View {
    func screenBackground() -> some View { background(Background()) }
}
