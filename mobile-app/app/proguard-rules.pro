# sherpa-onnx JNI 类保持不混淆（反射/JNI 调用）
-keep class com.k2fsa.sherpa.onnx.** { *; }
-dontwarn com.k2fsa.sherpa.onnx.**

# TRTC SDK（官方要求：不混淆，见 https://cloud.tencent.com/document/product/647/32175 步骤2）
-keep class com.tencent.** { *; }
-dontwarn com.tencent.**
