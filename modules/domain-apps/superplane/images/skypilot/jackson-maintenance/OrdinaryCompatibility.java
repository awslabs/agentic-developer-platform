import io.ray.shaded.com.fasterxml.jackson.annotation.JsonProperty;
import io.ray.shaded.com.fasterxml.jackson.core.*;
import io.ray.shaded.com.fasterxml.jackson.databind.*;
import io.ray.runtime.runtimeenv.RuntimeEnvImpl;
import com.github.fge.jsonschema.main.JsonSchemaFactory;
import java.util.*;
import java.util.jar.*;
import java.net.*;
import java.nio.file.*;

/** Ordinary compatibility of the installed shaded classes; no exploit fixtures. */
public final class OrdinaryCompatibility {
  public static final class Document {
    @JsonProperty("display_name") public String name;
    public List<Integer> numbers;
    public Document() {}
  }
  private static void require(boolean condition, String description) {
    if (!condition) throw new AssertionError(description);
  }
  public static void main(String[] args) throws Exception {
    ObjectMapper mapper = new ObjectMapper();
    require(mapper.version().toString().equals("2.18.11"), "databind fixed version");
    JsonFactory factory = JsonFactory.builder().enable(StreamReadFeature.USE_FAST_DOUBLE_PARSER).build();
    require(factory.version().toString().equals("2.18.11"), "core fixed version");
    Document input = new Document();
    input.name = "workspace-\u03b1"; input.numbers = Arrays.asList(1, 2, 3);
    String json = mapper.writeValueAsString(input);
    require(json.contains("display_name"), "annotation relocation");
    Document output = mapper.readValue(json, Document.class);
    require(input.name.equals(output.name) && input.numbers.equals(output.numbers), "POJO roundtrip");
    JsonNode tree = mapper.readTree("{\"workspace\":{\"enabled\":true},\"values\":[1,2,3]}");
    require(tree.path("workspace").path("enabled").asBoolean(), "tree parsing");
    require(tree.path("values").size() == 3, "array parsing");
    try (JsonParser parser = factory.createParser("[3.141592653589793,1.25e3,-0.0625]")) {
      require(parser.nextToken() == JsonToken.START_ARRAY, "stream start");
      parser.nextToken(); require(parser.getDoubleValue() == Math.PI, "ordinary fast double");
      parser.nextToken(); require(parser.getDoubleValue() == 1250.0, "ordinary exponent");
      parser.nextToken(); require(parser.getDoubleValue() == -0.0625, "ordinary fraction");
      require(parser.nextToken() == JsonToken.END_ARRAY && parser.nextToken() == null, "stream complete");
    }
    require(ServiceLoader.load(JsonFactory.class).iterator().next().version().toString().equals("2.18.11"), "shaded factory service");
    require(ServiceLoader.load(ObjectCodec.class).iterator().next().version().toString().equals("2.18.11"), "shaded mapper service");
    RuntimeEnvImpl runtime = new RuntimeEnvImpl();
    Map<String, String> variables = new HashMap<>(); variables.put("DEMO_NAME", "ordinary-review");
    runtime.set("env_vars", variables);
    require(runtime.contains("env_vars"), "Ray runtime environment set");
    require(mapper.readTree(runtime.serialize()).path("env_vars").path("DEMO_NAME").asText().equals("ordinary-review"), "Ray serialized environment");
    runtime.setJsonStr("env_vars", "{\"DEMO_NAME\":\"second-review\"}");
    require(runtime.getJsonStr("env_vars").contains("second-review"), "Ray JSON re-entry");
    require(runtime.GenerateRuntimeEnvInfo() != null, "Ray protobuf environment interoperability");
    JsonNode schema = mapper.readTree("{\"type\":\"object\",\"properties\":{\"workspace\":{\"type\":\"string\"}},\"required\":[\"workspace\"]}");
    require(JsonSchemaFactory.byDefault().getJsonSchema(schema).validate(mapper.readTree("{\"workspace\":\"demo\"}")).isSuccess(), "existing JSON schema consumer");
    String prefix = "io.ray.shaded.com.fasterxml.jackson.";
    Class<?> fast = Class.forName(prefix + "core.internal.shaded.fdp.v2_18_11.FastDoubleSwar");
    URL location = fast.getResource("FastDoubleSwar.class");
    require(location != null, "optimized parser resource");
    String expected = args.length > 1 ? args[1] : "META-INF/versions/17/";
    if (expected.equals("base")) require(!location.toString().contains("META-INF/versions/"), "base parser variant");
    else require(location.toString().contains(expected), "correct multi-release parser variant: " + location);
    int classes = 0;
    try (JarFile jar = new JarFile(args[0])) {
      Enumeration<JarEntry> entries = jar.entries();
      while (entries.hasMoreElements()) {
        String name = entries.nextElement().getName();
        if (name.startsWith("io/ray/shaded/com/fasterxml/jackson/") && name.endsWith(".class")) {
          require(!name.contains("v2_18_8"), "old bundled parser removed");
          Class.forName(name.substring(0, name.length() - 6).replace('/', '.'), false, OrdinaryCompatibility.class.getClassLoader());
          classes++;
        }
      }
    }
    require(classes > 1000, "complete shaded class loading");
    System.out.println("ordinary_compatibility=PASS core=2.18.11 databind=2.18.11 classes=" + classes + " variant=" + location);
  }
}
