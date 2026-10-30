import SwiftUI

/// Markdown split into prose paragraphs and fenced code blocks.
enum Block: Identifiable {
    case text(String)
    case code(language: String, body: String)

    var id: String {
        switch self {
        case .text(let s): "t\(s.hashValue)"
        case .code(let l, let s): "c\(l)\(s.hashValue)"
        }
    }

    static func parse(_ content: String) -> [Block] {
        var blocks: [Block] = []
        for (i, seg) in content.components(separatedBy: "```").enumerated() {
            if i % 2 == 1 {
                var lines = seg.components(separatedBy: "\n")
                var lang = ""
                if let first = lines.first, !first.contains(" ") {
                    lang = first.trimmingCharacters(in: .whitespaces)
                    lines.removeFirst()
                }
                blocks.append(.code(language: lang, body: lines.joined(separator: "\n").trimmingCharacters(in: .newlines)))
            } else {
                blocks += seg.components(separatedBy: "\n\n")
                    .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
                    .filter { !$0.isEmpty }
                    .map { .text($0) }
            }
        }
        return blocks
    }
}

struct MarkdownText: View {
    let content: String

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            ForEach(Block.parse(content)) { block in
                switch block {
                case .text(let t):
                    ProseBlock(text: t)
                case .code(let lang, let body):
                    CodeBlock(language: lang, code: body)
                }
            }
        }
    }

    static func attributed(_ s: String) -> AttributedString {
        let opts = AttributedString.MarkdownParsingOptions(interpretedSyntax: .inlineOnlyPreservingWhitespace)
        return (try? AttributedString(markdown: s, options: opts)) ?? AttributedString(s)
    }
}

/// One paragraph of prose: headings, bullet/numbered lists and quotes are
/// laid out per line; everything else is inline Markdown.
private struct ProseBlock: View {
    let text: String

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            ForEach(Array(text.components(separatedBy: "\n").enumerated()), id: \.offset) { _, line in
                lineView(line)
            }
        }
        .textSelection(.enabled)
        .fixedSize(horizontal: false, vertical: true)
    }

    @ViewBuilder private func lineView(_ raw: String) -> some View {
        let line = raw.trimmingCharacters(in: .whitespaces)
        let indent = CGFloat(raw.prefix { $0 == " " }.count / 2) * 14
        if let (level, rest) = Self.heading(line) {
            Text(MarkdownText.attributed(rest))
                .font(.system(size: level == 1 ? 20 : level == 2 ? 17 : 15, weight: .semibold))
                .padding(.top, 4)
        } else if let (marker, rest) = Self.listItem(line) {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Text(marker).foregroundStyle(.secondary).monospacedDigit()
                Text(MarkdownText.attributed(rest)).lineSpacing(4)
            }
            .font(.system(size: 14))
            .padding(.leading, indent)
        } else if line.hasPrefix(">") {
            Text(MarkdownText.attributed(String(line.dropFirst()).trimmingCharacters(in: .whitespaces)))
                .font(.system(size: 14))
                .foregroundStyle(.secondary)
                .padding(.leading, 12)
                .overlay(alignment: .leading) { Rectangle().fill(Color.primary.opacity(0.15)).frame(width: 2) }
        } else if line.allSatisfy({ $0 == "-" || $0 == "*" || $0 == "_" }) && line.count >= 3 {
            Divider().padding(.vertical, 4)
        } else {
            Text(MarkdownText.attributed(raw))
                .font(.system(size: 14))
                .lineSpacing(5)
        }
    }

    static func heading(_ line: String) -> (Int, String)? {
        let hashes = line.prefix { $0 == "#" }.count
        guard (1...6).contains(hashes), line.dropFirst(hashes).first == " " else { return nil }
        return (hashes, String(line.dropFirst(hashes + 1)))
    }

    static func listItem(_ line: String) -> (String, String)? {
        for b in ["- ", "* ", "+ "] where line.hasPrefix(b) {
            let rest = String(line.dropFirst(2))
            if rest.hasPrefix("[ ] ") { return ("☐", String(rest.dropFirst(4))) }
            if rest.hasPrefix("[x] ") || rest.hasPrefix("[X] ") { return ("☑", String(rest.dropFirst(4))) }
            return ("•", rest)
        }
        let digits = line.prefix { $0.isNumber }
        if !digits.isEmpty, digits.count <= 3 {
            let after = line.dropFirst(digits.count)
            if after.hasPrefix(". ") || after.hasPrefix(") ") {
                return ("\(digits).", String(after.dropFirst(2)))
            }
        }
        return nil
    }
}

struct CodeBlock: View {
    let language: String
    let code: String
    @State private var copied = false

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack {
                Text(language.isEmpty ? "code" : language)
                    .font(.system(size: 11))
                    .foregroundStyle(.tertiary)
                Spacer()
                Button(copied ? "Copied" : "Copy") {
                    Pasteboard.copy(code)
                    copied = true
                    Task { try? await Task.sleep(for: .seconds(1.5)); copied = false }
                }
                .buttonStyle(.plain)
                .font(.system(size: 11))
                .foregroundStyle(.secondary)
            }
            .padding(.horizontal, 14)
            .padding(.top, 8)
            ScrollView(.horizontal, showsIndicators: false) {
                Text(code)
                    .font(.system(size: 12.5, design: .monospaced))
                    .lineSpacing(4)
                    .textSelection(.enabled)
                    .padding(.horizontal, 14)
                    .padding(.vertical, 10)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: 10, style: .continuous).fill(Color.primary.opacity(0.035)))
        .overlay(RoundedRectangle(cornerRadius: 10, style: .continuous).strokeBorder(Color.primary.opacity(0.07), lineWidth: 0.5))
    }
}

struct IconButton: View {
    let symbol: String
    let help: String
    let action: () -> Void
    @State private var hover = false

    var body: some View {
        Button(action: action) {
            Image(systemName: symbol)
                .font(.system(size: 12))
                .frame(width: 26, height: 26)
                .foregroundStyle(hover ? .primary : .secondary)
                .background(RoundedRectangle(cornerRadius: 6).fill(hover ? Color.primary.opacity(0.05) : .clear))
        }
        .buttonStyle(.plain)
        .help(help)
        .onHover { hover = $0 }
    }
}

struct ChipStyle: ButtonStyle {
    var prominent = false
    @State private var hover = false

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.system(size: 13, weight: prominent ? .medium : .regular))
            .foregroundStyle(prominent ? AnyShapeStyle(.background) : AnyShapeStyle(.primary.opacity(0.85)))
            .padding(.horizontal, 14)
            .frame(height: 30)
            .background(Capsule().fill(
                prominent ? Color.primary.opacity(configuration.isPressed ? 0.75 : 1)
                    : (hover || configuration.isPressed ? Color.primary.opacity(0.05) : .clear)))
            .overlay(Capsule().strokeBorder(Color.primary.opacity(prominent ? 0 : 0.12), lineWidth: 0.5))
            .onHover { hover = $0 }
    }
}

struct TypingDots: View {
    var body: some View {
        TimelineView(.animation) { ctx in
            let t = ctx.date.timeIntervalSinceReferenceDate
            HStack(spacing: 5) {
                ForEach(0..<3) { i in
                    Circle()
                        .frame(width: 6, height: 6)
                        .opacity(0.25 + 0.75 * (0.5 + 0.5 * sin((t - Double(i) * 0.2) * .pi * 1.6)))
                }
            }
            .padding(.vertical, 6)
        }
    }
}

/// The Janus mark: a two-faced "J" on a soft tile, same in light and dark.
struct BrandMark: View {
    var size: CGFloat = 26

    var body: some View {
        RoundedRectangle(cornerRadius: size * 0.28, style: .continuous)
            .fill(Color.primary)
            .frame(width: size, height: size)
            .overlay(
                Text("J")
                    .font(.system(size: size * 0.5, weight: .semibold, design: .serif))
                    .foregroundStyle(.background))
    }
}

enum Pasteboard {
    static func copy(_ s: String) {
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(s, forType: .string)
    }
}
