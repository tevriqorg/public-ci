package tech.bogomolov.incomingsmsgateway;

import static org.junit.Assert.*;
import org.junit.Test;

public class AppSourceSearchTest {
    @Test public void matchesDisplayNameAndPackage() {
        assertTrue(AppSourceSearch.matches("微信", "com.tencent.mm", "微信"));
        assertTrue(AppSourceSearch.matches("微信", "com.tencent.mm", "TENCENT"));
        assertTrue(AppSourceSearch.matches("Mail", "com.example.mail", "example mail"));
        assertFalse(AppSourceSearch.matches("微信", "com.tencent.mm", "qq"));
    }
    @Test public void blankAndHiddenSelectionSearch() {
        assertTrue(AppSourceSearch.matches("Any", "com.example.any", " "));
        assertTrue(AppSourceSearch.matches("Any", "com.example.any", null));
        assertFalse(AppSourceSearch.matches(null, null, "missing"));
    }
}
