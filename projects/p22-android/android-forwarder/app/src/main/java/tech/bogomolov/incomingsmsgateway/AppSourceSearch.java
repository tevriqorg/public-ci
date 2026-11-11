package tech.bogomolov.incomingsmsgateway;

import java.util.Locale;

/** Pure, locale-safe app-name/package filtering for the local-only picker. */
public final class AppSourceSearch {
    private AppSourceSearch() { }

    public static boolean matches(String label, String packageName, String query) {
        if (query == null || query.trim().isEmpty()) return true;
        String name = label == null ? "" : label.toLowerCase(Locale.ROOT);
        String pkg = packageName == null ? "" : packageName.toLowerCase(Locale.ROOT);
        for (String word : query.toLowerCase(Locale.ROOT).trim().split("\\s+")) {
            if (!name.contains(word) && !pkg.contains(word)) return false;
        }
        return true;
    }
}
