const express = require('express');
const multer = require('multer');
const canvas = require('canvas');
const faceapi = require('face-api.js');
const path = require('path');
const fs = require('fs');

// 配置canvas环境
const { Canvas, Image, ImageData } = canvas;
faceapi.env.monkeyPatch({ Canvas, Image, ImageData });

const app = express();
const upload = multer({ storage: multer.memoryStorage() });

// 静态文件服务
app.use(express.static('.'));
app.use(express.json());

// 模型加载状态
let modelsLoaded = false;

// 加载人脸识别模型
async function loadModels() {
  if (modelsLoaded) return;
  
  try {
    console.log('开始加载人脸识别模型...');
    
    // 从CDN加载轻量级模型
    await faceapi.nets.tinyFaceDetector.loadFromUri('https://raw.githubusercontent.com/justadudewhohacks/face-api.js/master/weights');
    await faceapi.nets.faceLandmark68Net.loadFromUri('https://raw.githubusercontent.com/justadudewhohacks/face-api.js/master/weights');
    await faceapi.nets.faceRecognitionNet.loadFromUri('https://raw.githubusercontent.com/justadudewhohacks/face-api.js/master/weights');
    
    modelsLoaded = true;
    console.log('人脸识别模型加载完成');
  } catch (error) {
    console.error('模型加载失败:', error);
    throw error;
  }
}

// 提取人脸特征向量
async function extractFaceVector(imageBuffer) {
  const img = new Image();
  img.src = imageBuffer;
  
  const detections = await faceapi
    .detectSingleFace(img, new faceapi.TinyFaceDetectorOptions({ inputSize: 224, scoreThreshold: 0.2 }))
    .withFaceLandmarks()
    .withFaceDescriptor();
    
  if (!detections) {
    throw new Error('未检测到人脸');
  }
  
  return Array.from(detections.descriptor);
}

// 计算欧几里得距离
function euclideanDistance(a, b) {
  return Math.sqrt(a.reduce((sum, val, i) => sum + Math.pow(val - b[i], 2), 0));
}

// API路由

// 健康检查
app.get('/api/health', (req, res) => {
  res.json({ 
    status: 'ok', 
    modelsLoaded,
    timestamp: new Date().toISOString()
  });
});

// 初始化模型
app.post('/api/init', async (req, res) => {
  try {
    await loadModels();
    res.json({ success: true, message: '模型加载完成' });
  } catch (error) {
    res.status(500).json({ success: false, error: error.message });
  }
});

// 人脸注册
app.post('/api/register', upload.single('image'), async (req, res) => {
  try {
    if (!modelsLoaded) {
      await loadModels();
    }
    
    if (!req.file) {
      return res.status(400).json({ success: false, error: '未上传图片' });
    }
    
    const { name } = req.body;
    if (!name) {
      return res.status(400).json({ success: false, error: '未提供姓名' });
    }
    
    console.log(`开始处理 ${name} 的注册请求...`);
    const startTime = Date.now();
    
    const faceVector = await extractFaceVector(req.file.buffer);
    
    const endTime = Date.now();
    console.log(`特征提取完成，耗时: ${endTime - startTime}ms`);
    
    // 这里可以保存到数据库，示例使用文件存储
    const userData = {
      name,
      vector: faceVector,
      timestamp: Date.now()
    };
    
    // 读取现有数据
    let users = [];
    const dataFile = './face_data.json';
    if (fs.existsSync(dataFile)) {
      users = JSON.parse(fs.readFileSync(dataFile, 'utf8'));
    }
    
    users.push(userData);
    fs.writeFileSync(dataFile, JSON.stringify(users, null, 2));
    
    res.json({ 
      success: true, 
      message: `${name} 注册成功`,
      processingTime: endTime - startTime
    });
    
  } catch (error) {
    console.error('注册失败:', error);
    res.status(500).json({ success: false, error: error.message });
  }
});

// 人脸登录
app.post('/api/login', upload.single('image'), async (req, res) => {
  try {
    if (!modelsLoaded) {
      await loadModels();
    }
    
    if (!req.file) {
      return res.status(400).json({ success: false, error: '未上传图片' });
    }
    
    console.log('开始处理登录请求...');
    const startTime = Date.now();
    
    const faceVector = await extractFaceVector(req.file.buffer);
    
    // 读取用户数据
    const dataFile = './face_data.json';
    if (!fs.existsSync(dataFile)) {
      return res.status(404).json({ success: false, error: '没有注册用户' });
    }
    
    const users = JSON.parse(fs.readFileSync(dataFile, 'utf8'));
    
    // 查找最佳匹配
    let bestMatch = null;
    let minDistance = Infinity;
    
    for (const user of users) {
      const distance = euclideanDistance(faceVector, user.vector);
      console.log(`与 ${user.name} 的距离: ${distance}`);
      
      if (distance < minDistance) {
        minDistance = distance;
        bestMatch = user;
      }
    }
    
    const endTime = Date.now();
    const threshold = 0.6;
    
    if (minDistance < threshold) {
      res.json({ 
        success: true, 
        message: `欢迎回来，${bestMatch.name}`,
        user: bestMatch.name,
        confidence: (1 - minDistance).toFixed(3),
        processingTime: endTime - startTime
      });
    } else {
      res.status(401).json({ 
        success: false, 
        error: '未找到匹配用户',
        minDistance: minDistance.toFixed(3),
        processingTime: endTime - startTime
      });
    }
    
  } catch (error) {
    console.error('登录失败:', error);
    res.status(500).json({ success: false, error: error.message });
  }
});

// 清除数据
app.delete('/api/clear', (req, res) => {
  try {
    const dataFile = './face_data.json';
    if (fs.existsSync(dataFile)) {
      fs.unlinkSync(dataFile);
    }
    res.json({ success: true, message: '数据已清除' });
  } catch (error) {
    res.status(500).json({ success: false, error: error.message });
  }
});

// 启动服务器
const PORT = process.env.PORT || 3000;
app.listen(PORT, () => {
  console.log(`服务器运行在 http://localhost:${PORT}`);
  console.log('正在预加载人脸识别模型...');
  loadModels().catch(console.error);
});

module.exports = app;