package tech.bogomolov.incomingsmsgateway;

import static org.junit.Assert.assertFalse;

import org.junit.Test;

public class Pxt9CryptoInteropTest {
    @Test
    public void emitEnvelopeForBrowserInterop() throws Exception {
        String publicKey =
                "BIKTGDD9i4Qlkep9ZZPng6Ac_nknbOEcNkPvVeO_6uLRYdd5zwBsKiZTR_PqC4Zxvqmvg4O0InE1y-PxtjQVCA0";
        String envelope = Pxt9Crypto.encryptSms(
                "95588",
                "【PXT9互通测试】完整短信，验证码 381927，5 分钟内有效。",
                1700000000000L,
                "sim1",
                "interop-device",
                publicKey
        );
        assertFalse(envelope.contains("381927"));
        System.out.println("PXT9_INTEROP_ENVELOPE=" + envelope);
    }
}
