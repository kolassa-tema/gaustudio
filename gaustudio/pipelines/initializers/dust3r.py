import os
import numpy as np
import tempfile
import torch
import open3d as o3d
from typing import List, Tuple
from pathlib import Path
import PIL.Image

from gaustudio.pipelines import initializers
from gaustudio.pipelines.initializers.pcd import PcdInitializer
from gaustudio.datasets import Camera
from gaustudio.datasets.utils import focal2fov


from mini_dust3r.model import AsymmetricCroCo3DStereo
from mini_dust3r.inference import inference
from mini_dust3r.image_pairs import make_pairs
from mini_dust3r.cloud_opt import global_aligner, GlobalAlignerMode
from mini_dust3r.cloud_opt.base_opt import BasePCOptimizer
from mini_dust3r.viz import pts3d_to_trimesh, cat_meshes
from mini_dust3r.utils.image import load_images, ImgNorm

def _resize_pil_image(img, long_edge_size):
    S = max(img.size)
    if S > long_edge_size:
        interp = PIL.Image.LANCZOS
    elif S <= long_edge_size:
        interp = PIL.Image.BICUBIC
    new_size = tuple(int(round(x * long_edge_size / S)) for x in img.size)
    return img.resize(new_size, interp)

def combine_and_clean_point_clouds(pcds, max_points=500000):
    pcd_combined = o3d.geometry.PointCloud()
    for p3d in pcds:
        pcd_combined += p3d
    total_points = len(pcd_combined.points)
    if total_points > max_points:
        every_k = total_points // max_points
    else:
        every_k = 1
    pcd_combined = pcd_combined.uniform_down_sample(every_k)
    # cl, ind = pcd_combined.remove_statistical_outlier(nb_neighbors=20, std_ratio=2.0)
    # pcd_cleaned = pcd_combined.select_by_index(ind)
    
    return pcd_combined

@initializers.register('dust3r')
class Dust3rInitializer(PcdInitializer):
    def __init__(self, initializer_config):
        super().__init__(initializer_config)
        self.ws_dir = self.initializer_config.get('workspace_dir')
        if self.ws_dir is None:
            self.ws_dir = Path(tempfile.mkdtemp())
            print(f"No workspace directory provided. Using temporary directory: {self.ws_dir}")
        else:
            self.ws_dir = Path(self.ws_dir)

        os.makedirs(self.ws_dir, exist_ok=True)
        self.model_path = str(self.ws_dir / 'fused.ply')
        self.dust3r_model = AsymmetricCroCo3DStereo.from_pretrained(
            "nielsr/DUSt3R_ViTLarge_BaseDecoder_512_dpt"
        ).to("cuda")
        self.imgs = []
        self.poses = []
        self.intrinsics = []
        self.image_size = 512
        self.max_images = 20

    def __call__(self, model, dataset, overwrite=False):
        if not os.path.exists(self.model_path) or overwrite:
            self.cache_dataset(dataset)
            self.process_dataset()
        model = self.build_model(model)
        return model
    
    def cache_dataset(self, dataset, square_ok=False):
        if len(dataset) > self.max_images:
            print(f"Downsampling dataset to {self.max_images} images using interval-based selection.")
            interval = len(dataset) // self.max_images
            dataset = dataset[::interval][:self.max_images]  # Ensure we don't exceed max_images
        
        for i, camera in enumerate(dataset[:self.max_images]):
            img = PIL.Image.fromarray((camera.image.numpy() * 255).astype(np.uint8))
            original_W, original_H = img.size
            
            # Calculate original focal lengths
            original_fx = original_W / (2 * np.tan(camera.FoVx / 2))
            original_fy = original_H / (2 * np.tan(camera.FoVy / 2))

            if self.image_size == 224:
                # resize short side to 224 (then crop)
                img = _resize_pil_image(img, round(self.image_size * max(original_W / original_H, original_H / original_W)))
            else:
                # resize long side to 512
                img = _resize_pil_image(img, self.image_size)
            
            W, H = img.size
            cx, cy = W // 2, H // 2
            if self.image_size == 224:
                half = min(cx, cy)
                img = img.crop((cx - half, cy - half, cx + half, cy + half))
                W, H = img.size
            else:
                halfw, halfh = ((2 * cx) // 16) * 8, ((2 * cy) // 16) * 8
                if not (square_ok) and W == H:
                    halfh = 3 * halfw / 4
                img = img.crop((cx - halfw, cy - halfh, cx + halfw, cy + halfh))
                W, H = img.size

            # Adjust focal lengths based on the new image size
            fx = original_fx * (W / original_W)
            fy = original_fy * (H / original_H)

            img_tensor = torch.tensor(np.asarray(img))
            self.imgs.append(dict(
                img=ImgNorm(img)[None],
                unnorm_img=img_tensor[None],
                true_shape=np.int32([img.size[::-1]]),
                idx=len(self.imgs),
                instance=str(len(self.imgs)),
            ))

            pose = torch.linalg.inv(camera.extrinsics)
            self.poses.append(pose)

            # Cache intrinsics with adjusted focal lengths
            cx, cy = W // 2, H // 2
            intrinsic = torch.tensor([
                [fx, 0, cx],
                [0, fy, cy],
                [0, 0, 1]
            ])
            
            self.intrinsics.append(intrinsic)

        # Convert poses and intrinsics to tensors
        self.poses = torch.stack(self.poses)
        self.intrinsics = torch.stack(self.intrinsics)

    def process_dataset(self):
        pairs: List[Tuple[dict, dict]] = make_pairs(
            self.imgs, scene_graph="complete", prefilter=None, symmetrize=True
        )
        output = inference(pairs, self.dust3r_model, "cuda", batch_size=16)
        self.dust3r_model = None
        scene: BasePCOptimizer = global_aligner(
            dust3r_output=output, device="cuda", mode=GlobalAlignerMode.PointCloudOptimizer
        )

        # Preset poses and intrinsics
        scene.preset_pose(self.poses)
        scene.preset_focal([K.diagonal()[:2].mean() for K in self.intrinsics])
        scene.preset_principal_point([K[:2, 2] for K in self.intrinsics])
        
        scene.compute_global_alignment(
            init="known_poses", niter=500, schedule="cosine", lr=0.01
        )

        pts3d_list = [pt3d.numpy(force=True) for pt3d in scene.get_pts3d()]
        masks_list = [mask.numpy(force=True) for mask in scene.get_masks()]
        images_list = [img_dict['unnorm_img'].squeeze() for img_dict in self.imgs]

        pcds = []
        for pts, img, mask in zip(pts3d_list, images_list, masks_list):
            if mask.mean() == 0:
                continue
            pcd = o3d.geometry.PointCloud()
            pcd.points = o3d.utility.Vector3dVector(pts[mask].reshape(-1, 3))
            colors = (img[mask] / 255.0).cpu().numpy()
            pcd.colors = o3d.utility.Vector3dVector(colors.reshape(-1, 3))
            pcds.append(pcd)
        combined_pcd = combine_and_clean_point_clouds(pcds)   
        o3d.io.write_point_cloud(self.model_path, combined_pcd)
        print(f"Fused point cloud saved to {self.model_path}")