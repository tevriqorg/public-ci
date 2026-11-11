package tech.bogomolov.incomingsmsgateway;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.Arrays;
import java.util.HashSet;
import java.util.Locale;
import java.util.Set;

/**
 * Source selection and bounded text extraction for an explicitly opt-in
 * notification listener. No notification content enters diagnostic logs.
 */
public final class NotificationPolicy {
    private NotificationPolicy() { }

    public static boolean accepts(String sourcePackage, String ownPackage,
                                  boolean enabled, boolean isSummary,
                                  boolean isOngoing, String blockedCsv,
                                  String allowedCsv) {
        if (!enabled || sourcePackage == null || sourcePackage.isEmpty()
                || sourcePackage.equals(ownPackage) || isSummary || isOngoing) return false;
        String pkg = sourcePackage.trim().toLowerCase(Locale.ROOT);
        if (parsePackages(blockedCsv).contains(pkg)) return false;
        Set<String> allowed = parsePackages(allowedCsv);
        // Explicit but malformed allowlists must fail closed, not "allow all".
        if (allowedCsv != null && !allowedCsv.trim().isEmpty() && allowed.isEmpty())
            return false;
        return allowed.isEmpty() || allowed.contains(pkg);
    }

    static Set<String> parsePackages(String input) {
        Set<String> result = new HashSet<>();
        if (input == null) return result;
        for (String part : input.split("[,;\\s]+")) {
            String p = part.trim().toLowerCase(Locale.ROOT);
            if (p.matches("[a-z0-9_]+(\\.[a-z0-9_]+)+") && p.length() <= 220)
                result.add(p);
        }
        return result;
    }

    public static String bounded(CharSequence value, int max) {
        if (value == null || max <= 0) return "";
        String clean = value.toString().replace('\u0000', ' ').trim();
        return clean.length() <= max ? clean : clean.substring(0, max);
    }

    /** Hash only. No message body, title or app name is persisted as a dedup key. */
    public static String fingerprint(String pkg, String notificationKey, long postTime,
                                     String title, String body) {
        try {
            MessageDigest digest = MessageDigest.getInstance("SHA-256");
            for (String part : Arrays.asList(pkg, notificationKey,
                    String.valueOf(postTime), title, body)) {
                if (part != null) digest.update(part.getBytes(StandardCharsets.UTF_8));
                digest.update((byte) 0);
            }
            byte[] hash = digest.digest();
            StringBuilder result = new StringBuilder();
            for (int i = 0; i < 16; i++) {
                result.append(Character.forDigit((hash[i] >> 4) & 15, 16));
                result.append(Character.forDigit(hash[i] & 15, 16));
            }
            return result.toString();
        } catch (Exception e) {
            throw new IllegalStateException("SHA-256 unavailable", e);
        }
    }
}
