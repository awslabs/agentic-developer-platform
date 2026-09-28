import java.io.ByteArrayInputStream;
import java.nio.charset.StandardCharsets;
import org.apache.hc.core5.http.MessageConstraintException;
import org.apache.hc.core5.http.config.Http1Config;
import org.apache.hc.core5.http.impl.io.DefaultHttpResponseParser;
import org.apache.hc.core5.http.impl.io.SessionInputBufferImpl;

public class HttpCoreRegression {
    static void parse(String message) throws Exception {
        new DefaultHttpResponseParser().parse(new SessionInputBufferImpl(8192, Http1Config.DEFAULT.getMaxLineLength()),
            new ByteArrayInputStream(message.getBytes(StandardCharsets.US_ASCII)));
    }
    public static void main(String[] args) throws Exception {
        parse("HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n");
        for (String malicious : new String[] {
            "HTTP/1.1 200 OK\r\nX-Long: " + "a".repeat(20000) + "\r\n\r\n",
            "HTTP/1.1 200 OK\r\n" + "X-Header: value\r\n".repeat(200) + "\r\n"
        }) {
            try { parse(malicious); }
            catch (MessageConstraintException expected) { continue; }
            throw new AssertionError("unbounded headers accepted");
        }
        System.out.println("valid response accepted; oversized headers and header count rejected");
    }
}
