# 项目介绍
网页端同步播放双方本地视频

# 当前功能
创建房间随机生成房间号，可以上传本地视频及字幕，在同一个房间的人都准备好就可以同步播放

# 功能修正
1. 增加视频直链播放功能，视频直链播放可以从一个人共享到房间的其他用户，同样点击我已加载视频，双方都准备好即可同步播放
（参考/mnt/d/Code/mpv_synctv_v1的实现）
2. 增加弹幕功能
3. 将上述功能尽可能优雅布局，进行商业化设计的页面设计

/mnt/d/Code/codes/Project/synctv 能把相关逻辑添加到/mnt/d/Code/synctv_mine里，也增加一个登录bili和alist的功能，可以观看相关视频，重新设计整体页面，将/mnt/d/Code/codes/Project/synctv 的部分功能轻量化添加到/mnt/d/Code/synctv_mine里面